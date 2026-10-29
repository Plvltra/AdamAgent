"""文件编辑业务：先验证纯规划，后续阶段扩展确认、执行和真实工具链。"""

import hashlib
import json

import pytest

from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.edit_plan import EditConflictError, plan_edit
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot


@pytest.mark.parametrize("bom,newline", [(False, "\n"), (True, "\r\n"), (False, "\r\n")])
def test_read_version_and_edit_plan_preserve_bytes(tmp_path, bom, newline):
    path = tmp_path / "sample.txt"
    raw = ("中文\nvalue = 'old'\n尾行").replace("\n", newline).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)
    output = json.loads(
        create_tool_registry(tmp_path).execute("read_file", '{"path":"sample.txt"}')
    )
    assert output["version"] == hashlib.sha256(raw).hexdigest()
    plan = plan_edit(
        read_snapshot(path, 1024), "value = 'old'", 'value = "new"', output["version"], 1024
    )
    assert plan.candidate == raw.replace(b"value = 'old'", b'value = "new"')
    assert "-value = 'old'" in plan.diff and '+value = "new"' in plan.diff
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "body,old,new,limit",
    [
        ("abc", "", "x", 100),
        ("abc", "z", "x", 100),
        ("aaa", "aa", "x", 100),
        ("abc", "a", "a", 100),
        ("abc", "a", "long", 3),
        ("a\r\nb\n", "a", "x", 100),
        ("a\rb", "a", "x", 100),
    ],
)
def test_invalid_plans_leave_original_intact(tmp_path, body, old, new, limit):
    path = tmp_path / "sample.txt"
    raw = body.encode()
    path.write_bytes(raw)
    snapshot = read_snapshot(path, 1024)
    with pytest.raises(ToolError):
        plan_edit(snapshot, old, new, snapshot.version, limit)
    assert path.read_bytes() == raw


def test_stale_read_version_is_rejected(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"before")
    previous = read_snapshot(path, 1024)
    path.write_bytes(b"external")
    with pytest.raises(EditConflictError):
        plan_edit(read_snapshot(path, 1024), "external", "new", previous.version, 1024)
    assert path.read_bytes() == b"external"


@pytest.mark.parametrize("raw", [b"\xff", b"text\x00binary"])
def test_unsupported_files_cannot_form_edit_snapshots(tmp_path, raw):
    path = tmp_path / "sample.txt"
    path.write_bytes(raw)
    with pytest.raises(ToolError):
        read_snapshot(path, 1024)


def test_missing_final_newline_is_visible_in_diff(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"old")
    snapshot = read_snapshot(path, 1024)
    plan = plan_edit(snapshot, "old", "new", snapshot.version, 1024)
    assert "-old\n\\ No newline at end of file\n+new\n" in plan.diff
    assert plan.candidate == b"new"


def editing(tmp_path, confirm=None, permissions=None):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"\xef\xbb\xbfold\r\n")
    tool = EditTools(PathPolicy(tmp_path, permissions or PermissionConfig()), ToolLimits(), confirm)
    args = {
        "path": str(path),
        "old_text": "old",
        "new_text": "new",
        "expected_version": read_snapshot(path, 1024).version,
    }
    return path, tool, args


@pytest.mark.parametrize("approved", [False, True])
def test_edit_executes_only_the_approved_plan(tmp_path, approved):
    seen = []

    def confirm(plan):
        seen.append(plan)
        assert (tmp_path / "sample.txt").read_bytes() == b"\xef\xbb\xbfold\r\n"
        return approved

    path, tool, args = editing(tmp_path, confirm)
    result = tool.replace_text(args)
    assert len(seen) == 1 and "-old" in seen[0].diff and "+new" in seen[0].diff
    assert result["status"] == ("applied" if approved else "denied")
    assert path.read_bytes() == (b"\xef\xbb\xbfnew\r\n" if approved else b"\xef\xbb\xbfold\r\n")
    assert not list(tmp_path.glob(".adam-edit-*"))


def test_external_change_during_confirmation_invalidates_plan(tmp_path):
    def confirm(plan):
        plan.path.write_bytes(b"external")
        return True

    path, tool, args = editing(tmp_path, confirm)
    with pytest.raises(EditConflictError):
        tool.replace_text(args)
    assert path.read_bytes() == b"external"


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_write_failure_preserves_original_and_removes_temporary(tmp_path, monkeypatch, operation):
    import adam.tool_calls.edit_tools as edits

    path, tool, args = editing(tmp_path, lambda plan: True)

    def fail(*args):
        raise OSError("injected write boundary")

    monkeypatch.setattr(edits.os, operation, fail)
    with pytest.raises(ToolError):
        tool.replace_text(args)
    assert path.read_bytes() == b"\xef\xbb\xbfold\r\n"
    assert not list(tmp_path.glob(".adam-edit-*"))


def test_external_change_while_preparing_temporary_is_not_overwritten(tmp_path, monkeypatch):
    import adam.tool_calls.edit_tools as edits

    path, tool, args = editing(tmp_path, lambda plan: True)
    original_fsync = edits.os.fsync

    def change_after_flush(descriptor):
        original_fsync(descriptor)
        path.write_bytes(b"changed while preparing")

    monkeypatch.setattr(edits.os, "fsync", change_after_flush)
    with pytest.raises(EditConflictError):
        tool.replace_text(args)
    assert path.read_bytes() == b"changed while preparing"


@pytest.mark.parametrize("answer,approved", [("y", True), ("Y", True), ("yes", False), ("", False)])
def test_edit_confirmation_shows_exact_plan(tmp_path, answer, approved):
    from adam.cli.main import confirm_edit

    path, tool, args = editing(tmp_path)
    plan = tool.prepare(args)
    shown = []
    assert confirm_edit(plan, lambda prompt: answer, shown.append) is approved
    assert str(path) in shown[0] and shown[1] == plan.diff
    assert path.read_bytes() == b"\xef\xbb\xbfold\r\n"


def test_write_permissions_are_independent_and_git_is_protected(tmp_path):
    workspace, external = tmp_path / "work", tmp_path / "outside"
    workspace.mkdir()
    external.mkdir()
    path = external / "file.txt"
    path.write_bytes(b"old")
    policy = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(external,)))
    assert policy.resolve(str(path)) == path
    with pytest.raises(ToolError):
        policy.resolve_write(str(path))
    policy = PathPolicy(workspace, PermissionConfig(additional_write_dirs=(external,)))
    assert policy.resolve_write(str(path)) == path
    with pytest.raises(ToolError):
        policy.resolve(str(path))
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_bytes(b"old")
    with pytest.raises(ToolError):
        policy.resolve_write(".git/config")
    (workspace / ".env").write_bytes(b"secret")
    with pytest.raises(ToolError):
        policy.resolve_write(".env")

"""文件编辑业务：先验证纯规划，后续阶段扩展确认、执行和真实工具链。"""

import hashlib
import json

import pytest

from adam.tool_calls import ToolRegistry
from adam.tool_calls.edit_plan import EditConflictError, plan_edit
from adam.tool_calls.policy import ToolError
from adam.tool_calls.text_file import read_snapshot


@pytest.mark.parametrize("bom,newline", [(False, "\n"), (True, "\r\n"), (False, "\r\n")])
def test_read_version_and_edit_plan_preserve_bytes(tmp_path, bom, newline):
    path = tmp_path / "sample.txt"
    raw = ("中文\nvalue = 'old'\n尾行").replace("\n", newline).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)
    output = json.loads(ToolRegistry(tmp_path).execute("read_file", '{"path":"sample.txt"}'))
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

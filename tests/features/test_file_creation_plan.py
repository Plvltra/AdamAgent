"""B22.1.1：新文件规划明确缺失状态，真实检查权限但不创建目录/文件或请求批准。"""

import os
from dataclasses import FrozenInstanceError

import pytest

from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy


def tool(root, *, permissions=None, protected=(), maximum=1024):
    def no_approval(_plan):
        pytest.fail("planning must not request approval")

    return EditTools(
        PathPolicy(root, permissions or PermissionConfig(), protected_dirs=protected),
        ToolLimits(max_text_file_bytes=maximum),
        no_approval,
    )


def test_create_plan_is_immutable_utf8_lf_diff_without_filesystem_changes(tmp_path):
    plan = tool(tmp_path).prepare_create({"path": "note.txt", "content": "中文\r\nlast"})
    assert plan.path == tmp_path / "note.txt"
    assert plan.before_version is None and plan.original is None
    assert plan.candidate == "中文\nlast".encode()
    assert "--- /dev/null" in plan.diff and "+中文\n+last\n" in plan.diff
    assert "No newline at end of file" in plan.diff
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(FrozenInstanceError):
        plan.candidate = b"different"


def test_empty_create_remains_distinct_from_existing_empty_file(tmp_path):
    planner = tool(tmp_path)
    plan = planner.prepare_create({"path": "empty.txt", "content": ""})
    assert plan.candidate == b"" and plan.original is None and plan.before_version is None
    assert "new empty file" in plan.diff and "--- /dev/null" in plan.diff
    plan.path.write_bytes(b"")
    with pytest.raises(ToolError, match="已经存在"):
        planner.prepare_create({"path": "empty.txt", "content": "new"})
    assert plan.path.read_bytes() == b""


@pytest.mark.parametrize(
    "content,maximum", [("a\rb", 1024), ("nul\0", 1024), ("\ud800", 1024), ("汉", 2)]
)
def test_invalid_or_oversized_content_has_no_side_effect(tmp_path, content, maximum):
    with pytest.raises(ToolError):
        tool(tmp_path, maximum=maximum).prepare_create({"path": "note.txt", "content": content})
    assert list(tmp_path.iterdir()) == []


def test_missing_parent_and_existing_directory_are_not_created_or_replaced(tmp_path):
    planner = tool(tmp_path)
    with pytest.raises(ToolError, match="父目录"):
        planner.prepare_create({"path": "missing/note.txt", "content": "x"})
    assert not (tmp_path / "missing").exists()
    (tmp_path / "directory").mkdir()
    with pytest.raises(ToolError, match="已经存在"):
        planner.prepare_create({"path": "directory", "content": "x"})
    assert (tmp_path / "directory").is_dir()


@pytest.mark.parametrize(
    "path", [".git/HEAD", "llm.toml", ".env.local", "token.key", ".ssh/id_rsa", "private/item.txt"]
)
def test_create_plan_retains_credentials_git_and_reserved_directory_protection(tmp_path, path):
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(ToolError):
        tool(tmp_path, protected=(private,)).prepare_create({"path": path, "content": "x"})
    assert list(tmp_path.iterdir()) == [private] and list(private.iterdir()) == []


def test_read_authorization_does_not_authorize_creation_and_write_root_is_sufficient(tmp_path):
    workspace, outside = tmp_path / "work", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    path = outside / "new.txt"
    read_only = tool(workspace, permissions=PermissionConfig(additional_read_dirs=(outside,)))
    with pytest.raises(ToolError, match="additional_write_dirs"):
        read_only.prepare_create({"path": str(path), "content": "x"})
    writable = tool(workspace, permissions=PermissionConfig(additional_write_dirs=(outside,)))
    plan = writable.prepare_create({"path": str(path), "content": "x"})
    assert plan.path == path and not path.exists()
    with pytest.raises(ToolError, match="additional_read_dirs"):
        writable.policy.resolve(str(path), must_exist=False)


def test_windows_device_stream_and_ambiguous_names_are_refused(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-specific path aliases")
    for name in ("NUL", "CON.txt", "normal:stream", "trailing.", "trailing "):
        with pytest.raises(ToolError):
            tool(tmp_path).prepare_create({"path": name, "content": "x"})
    assert list(tmp_path.iterdir()) == []

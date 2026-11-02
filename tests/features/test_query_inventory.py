"""B21.1.1：真实目录扫描的顺序、权限、变化与取消；工具分页在下一小步接入。"""

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.contracts.file_query import QueryCursor, decode_cursor
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.query_scan import capture_inventory


def inventory(root, *, recursive=True, policy=None, maximum=5000, check=lambda: None):
    return capture_inventory(
        root,
        policy or PathPolicy(root, PermissionConfig()),
        recursive=recursive,
        max_entries=maximum,
        check=check,
    )


def test_scan_is_stably_sorted_and_old_generation_is_rejected_after_directory_change(tmp_path):
    for name in ("z.py", "a.py", "中文.py"):
        (tmp_path / name).write_text("needle")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.py").write_text("nested")
    first = inventory(tmp_path)
    assert [e.path for e in first.entries] == [
        ".",
        "a.py",
        "nested",
        "nested/b.py",
        "z.py",
        "中文.py",
    ]
    assert inventory(tmp_path) == first
    cursor = QueryCursor("a" * 64, first.generation, 50).encode()
    (nested / "new.py").write_text("added")
    changed = inventory(tmp_path)
    assert first.generation != changed.generation
    with pytest.raises(ValueError, match="来源已变化"):
        decode_cursor(cursor, "a" * 64, changed.generation)


def test_single_file_and_direct_directory_preserve_requested_scope(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("original")
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "HEAD").write_text("not recursively searchable")
    policy = PathPolicy(tmp_path, PermissionConfig())
    one = inventory(target, policy=policy)
    assert len(one.entries) == 1 and one.entries[0].kind == "file"
    direct = inventory(tmp_path, recursive=False)
    assert [e.path for e in direct.entries] == [".", ".git", "sample.py"]
    recursive = inventory(tmp_path)
    assert [e.path for e in recursive.entries] == [".", "sample.py"]
    target.write_text("new bytes and length")
    assert one.generation != inventory(target, policy=policy).generation


def test_scan_does_not_include_credentials_or_protected_material(tmp_path):
    protected = tmp_path / "private"
    protected.mkdir()
    (protected / "secret.py").write_text("private")
    (tmp_path / "llm.toml").write_text("secret")
    (tmp_path / "allowed.py").write_text("public")
    policy = PathPolicy(tmp_path, PermissionConfig(), protected_dirs=(protected,))
    found = inventory(tmp_path, policy=policy)
    assert [e.path for e in found.entries] == [".", "allowed.py"]
    assert found.skipped_entries == 2
    with pytest.raises(ToolError, match="保留目录"):
        inventory(protected, policy=policy)


def test_scan_limits_and_cancel_fail_instead_of_returning_partial_inventory(tmp_path):
    for i in range(3):
        (tmp_path / f"{i}.txt").write_text("data")
    with pytest.raises(ToolError, match="数量上限"):
        inventory(tmp_path, maximum=1)
    cancel = CancellationToken()
    checks = []

    def stop_during_scan():
        checks.append(True)
        if len(checks) == 3:
            cancel.cancel()
        cancel.check()

    with pytest.raises(RequestInterrupted):
        inventory(tmp_path, check=stop_during_scan)
    assert len(checks) == 3


def test_removed_source_is_not_a_successful_empty_directory(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    policy = PathPolicy(tmp_path, PermissionConfig())
    first = inventory(gone, policy=policy)
    gone.rmdir()
    assert first.entries[0].kind == "directory"
    with pytest.raises(ToolError, match="不存在"):
        inventory(gone, policy=policy)

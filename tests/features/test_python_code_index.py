"""T10.1：真实 Python 文件的结构、刷新、权限与资源边界；不导入目标项目。"""

import os
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

import adam.code_index.index as index_module
from adam.code_index.index import CodeIndex
from adam.code_index.models import CodeIndexError, CodeLimits
from adam.config.settings import PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.tool_calls.policy import PathPolicy


def make_index(root, **kwargs):
    """沿用生产路径策略；索引没有独立扩权入口。"""
    return CodeIndex(root, PathPolicy(root, PermissionConfig()).resolve, **kwargs)


def test_nested_repeated_definitions_import_aliases_and_source_locations(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = package / "mod.py"
    text = (
        '"""Module documentation."""\n'
        "import os.path\n"
        "import json as codec\n"
        "from .helpers import work as run\n"
        "from .. import parent\n"
        "class First:\n"
        "    def repeat(self):\n"
        "        def nested():\n"
        "            from pkg.helpers import work as local_work\n"
        "            return local_work()\n"
        "        return nested()\n"
        "class Second:\n"
        "    async def repeat(self):\n"
        "        return 1\n"
        "def repeat():\n"
        "    return 2\n"
        "if True:\n"
        "    def repeat():\n"
        "        return 3\n"
    )
    source.write_bytes(text.encode("utf-8"))
    snapshot = make_index(tmp_path).refresh()
    module = next(item for item in snapshot.modules if item.module == "pkg.mod")
    assert module.version == sha256(source.read_bytes()).hexdigest()
    assert module.issue is None and module.relative_path == "pkg/mod.py"
    repeats = snapshot.find_definitions("repeat")
    assert len(repeats) == len({item.identifier for item in repeats}) == 4
    assert [(item.start_line, item.end_line) for item in repeats] == [
        (7, 11),
        (13, 14),
        (15, 16),
        (18, 19),
    ]
    assert repeats[1].kind == "async_function"
    first = snapshot.find_definitions("pkg.mod.First")[0]
    assert repeats[0].scope_id == first.identifier
    nested = snapshot.find_definitions("nested")[0]
    assert nested.scope_id == repeats[0].identifier
    assert snapshot.find_definitions(nested.identifier) == (nested,)
    assert module.definitions[0].summary == "Module documentation."
    imports = {item.local_name: item for item in module.imports}
    assert imports["os"].target_module == "os.path" and imports["os"].imported_name is None
    assert imports["codec"].target_module == "json"
    assert (imports["run"].target_module, imports["run"].imported_name, imports["run"].level) == (
        "helpers",
        "work",
        1,
    )
    assert (imports["parent"].target_module, imports["parent"].level) == ("", 2)
    assert imports["local_work"].scope_id == nested.identifier
    assert imports["local_work"].start_line == 9
    assert {item.version for item in (*module.definitions, *module.imports)} == {module.version}


def test_same_name_in_different_modules_is_not_merged(tmp_path):
    for directory in ("left", "right"):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "same.py").write_text("def resolve():\n    pass\n", encoding="utf-8")
    found = make_index(tmp_path).refresh().find_definitions("resolve")
    assert len(found) == 2
    assert {item.qualified_name for item in found} == {"left.same.resolve", "right.same.resolve"}
    assert len({item.identifier for item in found}) == 2


def test_fresh_bytes_detect_same_timestamp_change_delete_and_reuse_unchanged_parse(
    tmp_path, monkeypatch
):
    source = tmp_path / "code.py"
    source.write_text("def first():\n    pass\n", encoding="utf-8")
    calls = []
    original = index_module.parse_module

    def parse(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(index_module, "parse_module", parse)
    index = make_index(tmp_path)
    first = index.refresh()
    assert index.refresh() == first and len(calls) == 1
    old_stat = source.stat()
    source.write_text("def later():\n    pass\n", encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    second = index.refresh()
    assert len(calls) == 2 and second.generation != first.generation
    assert not second.find_definitions("first") and second.find_definitions("later")
    source.unlink()
    deleted = index.refresh()
    assert not deleted.modules and not deleted.find_definitions("later")
    assert deleted.generation != second.generation


@pytest.mark.parametrize(
    "broken,reason",
    [(b"def broken(:\n", "syntax_error"), (b"\xff\xfe\x00", "encoding_error")],
)
def test_invalid_updated_source_replaces_old_definitions_with_diagnostic(tmp_path, broken, reason):
    source = tmp_path / "code.py"
    source.write_text("def old():\n    pass\n", encoding="utf-8")
    index = make_index(tmp_path)
    assert index.refresh().find_definitions("old")
    source.write_bytes(broken)
    failed = index.refresh()
    assert not failed.find_definitions("old")
    assert not failed.modules[0].definitions and not failed.modules[0].imports
    assert failed.modules[0].issue.reason == reason
    assert failed.modules[0].version == sha256(broken).hexdigest()
    source.write_bytes(b"\xef\xbb\xbfdef fixed():\r\n    pass\r\n")
    assert index.refresh().find_definitions("fixed")


def test_scanning_never_executes_top_level_code_and_skips_reserved_directories(tmp_path):
    source = tmp_path / "side_effect.py"
    marker = tmp_path / "must_not_exist"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\ndef safe():\n    pass\n",
        encoding="utf-8",
    )
    for name in (".git", ".venv", "__pycache__", "node_modules", "private"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "hidden.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    policy = PathPolicy(tmp_path, PermissionConfig(), protected_dirs=(tmp_path / "private",))
    snapshot = CodeIndex(tmp_path, policy.resolve).refresh()
    assert len(snapshot.modules) == 1 and snapshot.find_definitions("safe")
    assert not snapshot.find_definitions("hidden")
    assert snapshot.skipped_entries == 5 and not marker.exists()
    assert {item.name for item in tmp_path.iterdir()} == {
        "side_effect.py",
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        "private",
    }


@pytest.mark.parametrize(
    "limits",
    [
        CodeLimits(max_scan_entries=1),
        CodeLimits(max_files=1),
        CodeLimits(max_file_bytes=100, max_total_bytes=100),
        CodeLimits(max_symbols=2),
    ],
    ids=["entries", "files", "total-bytes", "symbols"],
)
def test_whole_scan_limits_raise_instead_of_returning_partial_or_cached_result(tmp_path, limits):
    source = tmp_path / "a.py"
    source.write_text("def one():\n    pass\n" + "#" * 60, encoding="utf-8")
    index = make_index(tmp_path, limits=limits)
    assert index.refresh().find_definitions("one")
    (tmp_path / "b.py").write_text("def two():\n    pass\n" + "#" * 60, encoding="utf-8")
    with pytest.raises(CodeIndexError):
        index.refresh()
    source.unlink()
    current = index.refresh()
    assert not current.find_definitions("one") and current.find_definitions("two")


@pytest.mark.parametrize(
    "limits,reason",
    [
        (CodeLimits(max_file_bytes=8), "file_too_large"),
        (CodeLimits(max_ast_nodes=3), "resource_limit"),
        (CodeLimits(max_ast_depth=1), "resource_limit"),
        (CodeLimits(max_symbols=1), "resource_limit"),
    ],
    ids=["file-bytes", "ast-nodes", "ast-depth", "file-symbols"],
)
def test_file_resource_failure_has_no_partial_definition_or_false_version(tmp_path, limits, reason):
    source = tmp_path / "code.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    module = make_index(tmp_path, limits=limits).refresh().modules[0]
    assert module.issue.reason == reason and not module.definitions
    assert (module.version is None) is (reason == "file_too_large")


def test_authorization_is_checked_again_and_explicit_external_root_needs_permission(tmp_path):
    workspace, outside = tmp_path / "project", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "a.py").write_text("def local():\n    pass\n", encoding="utf-8")
    (outside / "b.py").write_text("def remote():\n    pass\n", encoding="utf-8")
    policy = PathPolicy(workspace, PermissionConfig())
    revoked = False

    def authorize(value):
        if revoked:
            raise ToolError("revoked by host")
        return policy.resolve(value)

    index = CodeIndex(workspace, authorize)
    assert index.refresh().find_definitions("local")
    revoked = True
    with pytest.raises(ToolError, match="revoked"):
        index.refresh()
    with pytest.raises(ToolError, match="未授权"):
        CodeIndex(outside, policy.resolve).refresh()
    allowed = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(outside,)))
    assert CodeIndex(outside, allowed.resolve).refresh().find_definitions("remote")


def test_read_failure_clears_cached_symbols_and_can_recover(tmp_path, monkeypatch):
    source = tmp_path / "code.py"
    source.write_text("def old():\n    pass\n", encoding="utf-8")
    index = make_index(tmp_path)
    assert index.refresh().find_definitions("old")
    original = Path.open

    def fail(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb":
            raise PermissionError("simulated OS denial during file open")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail)
        current = index.refresh()
        assert current.modules[0].issue.reason == "read_error"
        assert not current.find_definitions("old") and current.modules[0].version is None
    assert index.refresh().find_definitions("old")


def test_timeout_and_cancel_do_not_return_previous_snapshot(tmp_path, monkeypatch):
    (tmp_path / "code.py").write_text("def work():\n    pass\n", encoding="utf-8")
    token = CancellationToken()
    index = make_index(tmp_path, check_cancel=token.check)
    assert index.refresh().find_definitions("work")
    ticks = iter((0, 10))
    with monkeypatch.context() as patch:
        patch.setattr(index_module.time, "monotonic", lambda: next(ticks))
        with pytest.raises(CodeIndexError, match="时间"):
            index.refresh()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        index.refresh()


def test_scan_does_not_follow_directory_links_even_when_target_is_authorized(tmp_path):
    workspace, outside = tmp_path / "project", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "external.py").write_text("def external():\n    pass\n", encoding="utf-8")
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path link -Target ..\\outside | Out-Null",
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    policy = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(outside,)))
    snapshot = CodeIndex(workspace, policy.resolve).refresh()
    assert not snapshot.modules and snapshot.skipped_entries == 1
    assert CodeIndex(outside, policy.resolve).refresh().find_definitions("external")


@pytest.mark.parametrize(
    "change",
    [
        {"max_files": True},
        {"max_ast_depth": 0},
        {"timeout_seconds": float("inf")},
        {"max_file_bytes": 100, "max_total_bytes": 50},
    ],
)
def test_invalid_limits_are_rejected_before_scanning(change):
    with pytest.raises(ValueError):
        replace(CodeLimits(), **change)


def test_encoding_declaration_cannot_silently_change_source_interpretation(tmp_path):
    source = tmp_path / "code.py"
    body = "def 示例():\n    pass\n".encode()
    index = make_index(tmp_path)
    source.write_bytes(b"# coding: latin-1\n" + body)
    assert index.refresh().modules[0].issue.reason == "encoding_error"
    source.write_bytes(b"# coding: utf8\n" + body)
    assert index.refresh().find_definitions("示例")
    source.write_bytes(b"\xef\xbb\xbf# coding: latin-1\n" + body)
    assert index.refresh().modules[0].issue.reason == "encoding_error"

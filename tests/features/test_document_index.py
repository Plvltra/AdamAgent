"""E2.2：真实文件更新、重启、并发刷新和保存故障保护。"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import FileLock

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.retrieval import keyword_search
from adam.tool_calls.policy import PathPolicy


def make_index(root, config=None):
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    config = config or DocumentConfig(roots=(docs,))
    return DocumentIndex(root / "index", root, config, PathPolicy(root, PermissionConfig()).resolve)


def test_refresh_reopen_same_mtime_and_deleted_source(tmp_path):
    index = make_index(tmp_path)
    source = tmp_path / "docs" / "policy.md"
    source.write_text("# 重试\n\n失败最多重试 2 次。", encoding="utf-8")
    first = index.refresh()
    assert "2 次" in keyword_search(first.chunks, "失败重试")[0].chunk.text
    stamp = source.stat()
    source.write_text("# 重试\n\n失败最多重试 3 次。", encoding="utf-8")
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = make_index(tmp_path).refresh()
    assert second.generation != first.generation
    assert "3 次" in keyword_search(second.chunks, "失败重试")[0].chunk.text
    assert first.chunks[1].version != second.chunks[1].version
    cached = index.path.stat().st_mtime_ns
    assert index.refresh() == second
    assert index.path.stat().st_mtime_ns == cached
    source.unlink()
    assert index.refresh().chunks == ()


@pytest.mark.parametrize("boundary", ["replace", "fsync"])
def test_failed_refresh_keeps_previous_snapshot_but_never_returns_it(
    tmp_path, monkeypatch, boundary
):
    index = make_index(tmp_path)
    source = tmp_path / "docs" / "guide.md"
    source.write_text("旧版内容", encoding="utf-8")
    index.refresh()
    previous = index.path.read_bytes()
    source.write_text("新版内容", encoding="utf-8")

    def fail(*args):
        raise OSError("injected persistence failure")

    monkeypatch.setattr("adam.documents.snapshots.os." + boundary, fail)
    with pytest.raises(DocumentError, match="未返回旧资料"):
        index.refresh()
    assert index.path.read_bytes() == previous
    assert list(index.path.parent.glob("*.tmp")) == []


def test_corruption_is_visible_and_preserved(tmp_path):
    index = make_index(tmp_path)
    index.refresh()
    index.path.write_text('{"version": 100}', encoding="utf-8")
    with pytest.raises(DocumentError, match="损坏"):
        index.refresh()
    assert json.loads(index.path.read_text()) == {"version": 100}


def test_read_failure_does_not_remove_previous_entries(tmp_path):
    index = make_index(tmp_path)
    source = tmp_path / "docs" / "guide.md"
    source.write_text("有效文本", encoding="utf-8")
    index.refresh()
    previous = index.path.read_bytes()
    source.write_bytes(b"\xff\xfe")
    with pytest.raises(DocumentError, match="UTF-8"):
        index.refresh()
    assert index.path.read_bytes() == previous


def test_scope_exclusions_and_scan_limits(tmp_path):
    index = make_index(tmp_path)
    docs = tmp_path / "docs"
    (docs / "visible.md").write_text("visible", encoding="utf-8")
    for name in (".git", ".venv", "node_modules", "__pycache__"):
        (docs / name).mkdir()
        (docs / name / "secret.md").write_text("secret", encoding="utf-8")
    (docs / "data.py").write_text("not a document", encoding="utf-8")
    assert index.refresh().file_count == 1
    (docs / "second.txt").write_text("more", encoding="utf-8")
    limited = make_index(tmp_path, replace(index.config, max_files=1))
    with pytest.raises(DocumentError, match="数量超限"):
        limited.refresh()
    assert index.path.read_bytes() == limited.path.read_bytes()


def test_lock_contention_reports_failure(tmp_path):
    index = make_index(tmp_path)
    index.refresh()
    index.lock.timeout = 0
    with FileLock(index.lock.lock_file):
        with pytest.raises(DocumentError, match="其他进程"):
            index.refresh()


def test_concurrent_processes_produce_one_valid_snapshot(tmp_path):
    make_index(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text("并发文档内容", encoding="utf-8")
    script = (
        "from pathlib import Path; "
        "from adam.config.settings import DocumentConfig, PermissionConfig; "
        "from adam.documents.index import DocumentIndex; "
        "from adam.tool_calls.policy import PathPolicy; "
        "import sys; "
        "p=Path(sys.argv[1]); "
        "i=DocumentIndex(p/'index',p,DocumentConfig(roots=(p/'docs',)),"
        "PathPolicy(p,PermissionConfig()).resolve); "
        "print(i.refresh().generation)"
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        outputs = [worker.communicate(timeout=20) for worker in workers]
        assert all(worker.returncode == 0 for worker in workers), outputs
        assert outputs[0][0] == outputs[1][0]
        assert len(make_index(tmp_path).refresh().chunks) == 1
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)


def test_config_bounds_and_outside_root_denial(tmp_path):
    from adam.contracts.errors import ConfigError, ToolError

    with pytest.raises(ConfigError):
        DocumentConfig(top_k=True)
    index = make_index(tmp_path, DocumentConfig(roots=(tmp_path.parent,)))
    with pytest.raises(ToolError, match="未授权"):
        index.refresh()

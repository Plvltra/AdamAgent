"""E1.1：独立记忆跨进程保留、范围隔离与写入失败保护。"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemorySource
from adam.memory.project import project_identity
from adam.memory.store import MemoryStore

SOURCE = MemorySource("add", "1234abcd")


def test_memory_versions_survive_restart_and_scope_isolation(tmp_path):
    project = str(tmp_path / "project")
    other = str(tmp_path / "other")
    root = tmp_path / "memory"
    store = MemoryStore(root)
    first = store.add("Python 使用绝对导入", project, SOURCE)
    global_record = store.add("回答使用中文", None, SOURCE)
    store.add("别的项目", other, SOURCE)
    changed = store.update(
        first.memory_id, project, "Python 使用明确的绝对导入", SOURCE, expected_revision=1
    )
    restarted = MemoryStore(root)
    assert restarted.list_records(project) == [changed]
    assert restarted.list_records(project, include_global=True) == [changed, global_record]
    assert restarted.history(first.memory_id, project) == (first, changed)
    with pytest.raises(MemoryInputError, match="没有"):
        restarted.delete(first.memory_id, other, SOURCE, expected_revision=2)
    restarted.delete(first.memory_id, project, SOURCE, expected_revision=2)
    assert MemoryStore(root).list_records(project) == []
    assert restarted.history(first.memory_id, project)[-1].source.operation == "delete"
    assert not list(tmp_path.rglob("*.jsonl"))


def test_stale_update_is_rejected_without_losing_other_terminal_change(tmp_path):
    store = MemoryStore(tmp_path)
    record = store.add("old", None, SOURCE)
    other = MemoryStore(tmp_path)
    other.update(record.memory_id, None, "other", SOURCE, expected_revision=1)
    with pytest.raises(MemoryInputError, match="已被其他"):
        store.update(record.memory_id, None, "stale", SOURCE, expected_revision=1)
    assert store.list_records(None)[0].content == "other"


def test_two_processes_preserve_all_additions(tmp_path):
    # 真实独立进程共享同一个文件锁，避免线程测试漏掉进程间丢更新。
    script = """
import sys
from pathlib import Path
from adam.memory.store import MemoryStore
from adam.contracts.memory import MemorySource
store = MemoryStore(Path(sys.argv[1]))
for index in range(12):
    store.add(f"{sys.argv[2]}:{index}", None, MemorySource("add"))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("A", "B")
    ]
    try:
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    assert {record.content for record in MemoryStore(tmp_path).list_records(None)} == {
        f"{name}:{index}" for name in ("A", "B") for index in range(12)
    }


def test_replace_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    store.add("original", None, SOURCE)
    before = store.path.read_bytes()

    def fail_replace(*args):
        raise PermissionError("injected atomic replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(MemoryStoreError):
        store.add("not saved", None, SOURCE)
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "raw", [b"{broken", b'{"version":2,"records":[]}', b'{"version":true,"records":[]}']
)
def test_corruption_is_not_treated_as_empty_and_never_overwritten(tmp_path, raw):
    store = MemoryStore(tmp_path)
    store.path.write_bytes(raw)
    with pytest.raises(MemoryStoreError):
        store.list_records(None)
    with pytest.raises(MemoryStoreError):
        store.add("not saved", None, SOURCE)
    assert store.path.read_bytes() == raw


def test_invalid_revision_chain_is_rejected(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("original", None, SOURCE)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["records"][0]["revision"] = 2
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MemoryStoreError):
        store.list_records(None)


def test_expiry_is_visible_but_not_active(tmp_path):
    store = MemoryStore(tmp_path)
    record = store.add("temporary", None, SOURCE, expires_at="2020-01-01T00:00:00+00:00")
    assert store.list_records(None) == [record]
    assert not record.active(datetime.now(UTC))
    with pytest.raises(MemoryInputError):
        store.add("", None, SOURCE)
    with pytest.raises(MemoryInputError):
        store.add("invalid time", None, SOURCE, expires_at="2030-01-01T00:00:00")


def test_project_identity_uses_nearest_git_marker_or_workspace(tmp_path):
    root = tmp_path / "repo"
    nested = root / "src"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    assert project_identity(nested) == os.path.normcase(str(root.resolve()))
    (nested / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert project_identity(nested) == os.path.normcase(str(nested.resolve()))
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    assert project_identity(standalone) == os.path.normcase(str(standalone.resolve()))

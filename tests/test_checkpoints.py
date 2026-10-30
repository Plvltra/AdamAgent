"""T7.1 检查点格式与存储边界；业务来源和重启验收见 features。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import FileLock

from adam.contracts.checkpoints import EditCheckpoint
from adam.contracts.errors import StorageError
from adam.session.checkpoints import CheckpointStore


def snapshot(tmp_path, original=b"old", **changes):
    value = EditCheckpoint(
        "12345678",
        "a" * 32,
        "b" * 32,
        "c" * 32,
        "call-edit",
        tmp_path / "source.txt",
        original,
        hashlib.sha256(original).hexdigest(),
        hashlib.sha256(b"new").hexdigest(),
    )
    return replace(value, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": "../escape"},
        {"edit_id": "x"},
        {"turn_id": ""},
        {"request_id": "x" * 32},
        {"tool_call_id": "\0"},
        {"tool_call_id": "x" * 1025},
        {"path": Path("relative.txt")},
        {"before_version": "0" * 64},
        {"target_version": hashlib.sha256(b"old").hexdigest()},
        {"original": bytearray(b"old")},
    ],
)
def test_contract_rejects_invalid_identity_or_version(tmp_path, changes):
    with pytest.raises(ValueError):
        snapshot(tmp_path, **changes)


def test_put_is_immutable_and_idempotent(tmp_path):
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path)
    store.put(item)
    path = store.root / item.session_id / f"{item.edit_id}.json"
    before = path.read_bytes()
    CheckpointStore(store.root, max_bytes=100).put(item)
    assert path.read_bytes() == before
    with pytest.raises(StorageError, match="拒绝覆盖"):
        store.put(replace(item, target_version=hashlib.sha256(b"other").hexdigest()))
    assert path.read_bytes() == before and store.load(item.session_id, item.edit_id) == item


@pytest.mark.parametrize(
    "corruption",
    [
        "version",
        "boolean_version",
        "extra",
        "missing",
        "duplicate",
        "base64",
        "hash",
        "session",
        "edit",
        "truncated",
        "oversized",
        "deep",
    ],
)
def test_corrupt_snapshot_is_not_returned(tmp_path, corruption):
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path)
    store.put(item)
    path = store.root / item.session_id / f"{item.edit_id}.json"
    payload = json.loads(path.read_bytes())
    if corruption == "version":
        payload["version"] = 2
    elif corruption == "boolean_version":
        payload["version"] = True
    elif corruption == "extra":
        payload["status"] = "applied"
    elif corruption == "missing":
        del payload["turn_id"]
    elif corruption == "base64":
        payload["original"] = "!!!"
    elif corruption == "hash":
        payload["before_version"] = "0" * 64
    elif corruption in {"session", "edit"}:
        payload[f"{corruption}_id"] = "d" * (8 if corruption == "session" else 32)
    encoded = json.dumps(payload)
    if corruption == "duplicate":
        encoded = encoded[:-1] + ', "version": 1}'
    elif corruption == "truncated":
        encoded = encoded[:-5]
    elif corruption == "oversized":
        encoded = " " * 70000
    elif corruption == "deep":
        encoded = "[" * 2000 + "0" + "]" * 2000
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(StorageError, match="损坏|格式"):
        store.load(item.session_id, item.edit_id)


def test_size_limit_is_applied_before_save_and_after_decode(tmp_path):
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path, original=b"x" * 101)
    with pytest.raises(StorageError, match="字节上限"):
        store.put(item)
    assert not store.root.exists()
    CheckpointStore(store.root, max_bytes=101).put(item)
    with pytest.raises(StorageError, match="损坏"):
        store.load(item.session_id, item.edit_id)


def test_independent_store_uses_lock_for_reads_and_writes(tmp_path):
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path)
    store.put(item)
    second = CheckpointStore(store.root, max_bytes=100)
    with FileLock(store.root / ".locks" / f"{item.session_id}.lock"):
        with pytest.raises(StorageError, match="正在访问"):
            second.load(item.session_id, item.edit_id)
        with pytest.raises(StorageError, match="正在访问"):
            second.put(item)
    assert second.load(item.session_id, item.edit_id) == item


def test_missing_and_orphan_temporary_do_not_become_snapshots(tmp_path):
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path)
    directory = store.root / item.session_id
    directory.mkdir(parents=True)
    pending = directory / ".checkpoint-orphan.tmp"
    pending.write_bytes(b'{"status":"applied"}')
    with pytest.raises(StorageError):
        store.load(item.session_id, item.edit_id)
    assert pending.read_bytes() == b'{"status":"applied"}'
    with pytest.raises(ValueError):
        store.load("../escape", item.edit_id)
    assert not (tmp_path / "source.txt").exists()


def test_resolved_escape_is_rejected_before_file_access(tmp_path, monkeypatch):
    """模拟路径解析发现链接越界，不依赖 Windows 创建符号链接的权限。"""
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=100)
    item = snapshot(tmp_path)
    store.put(item)
    real_resolve = Path.resolve
    target = store.root / item.session_id

    def resolve(path, *args, **kwargs):
        if path == target / f"{item.edit_id}.json":
            return tmp_path / "outside" / "secret.json"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(StorageError, match="超出"):
        store.load(item.session_id, item.edit_id)

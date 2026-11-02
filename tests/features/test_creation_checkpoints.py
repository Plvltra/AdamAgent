"""B22.2.1：缺失状态检查点与创建日志投影；不以快照存在冒充实际创建成功。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from adam.contracts.checkpoints import EditCheckpoint
from adam.contracts.errors import StorageError, ToolError
from adam.session.checkpoints import CheckpointStore
from adam.session.edit_log import EditLog
from adam.session.storage import SessionStore
from adam.session.undo_log import UndoLog
from adam.tool_calls.edit_plan import plan_restore
from adam.tool_calls.text_file import read_snapshot
from tests.features.test_edit_checkpoints import planned_edit


def creation(root, **changes):
    value = EditCheckpoint(
        "12345678",
        "a" * 32,
        "b" * 32,
        "c" * 32,
        "create-call",
        root / "new.txt",
        None,
        None,
        hashlib.sha256(b"").hexdigest(),
        "create",
    )
    return replace(value, **changes)


def test_absent_checkpoint_round_trip_never_accesses_target_or_becomes_empty_content(
    tmp_path, monkeypatch
):
    item = creation(tmp_path)
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=10)
    real_open = Path.open

    def no_target_open(path, *args, **kwargs):
        if path == item.path:
            pytest.fail("checkpoint must not inspect or create target")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_target_open)
    store.put(item)
    path = store.root / item.session_id / f"{item.edit_id}.json"
    encoded = path.read_bytes()
    payload = json.loads(encoded)
    assert payload["version"] == 2 and payload["operation"] == "create"
    assert payload["original"] is None and payload["before_version"] is None
    restarted = CheckpointStore(store.root, max_bytes=10)
    assert restarted.load(item.session_id, item.edit_id) == item
    restarted.put(item)
    assert path.read_bytes() == encoded and not item.path.exists()
    with pytest.raises(StorageError, match="拒绝覆盖"):
        restarted.put(replace(item, target_version=hashlib.sha256(b"other").hexdigest()))
    assert path.read_bytes() == encoded


@pytest.mark.parametrize(
    "changes",
    [
        {"original": b""},
        {"before_version": hashlib.sha256(b"").hexdigest()},
        {"operation": "replace"},
        {"operation": "delete"},
        {"target_version": None},
    ],
)
def test_contract_refuses_ambiguous_absent_state(tmp_path, changes):
    with pytest.raises(ValueError):
        creation(tmp_path, **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "replace"),
        ("operation", None),
        ("original", ""),
        ("before_version", hashlib.sha256(b"").hexdigest()),
        ("version", 1),
        ("version", True),
    ],
)
def test_corrupted_create_snapshot_cannot_be_read_as_replace(tmp_path, field, value):
    item = creation(tmp_path)
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=10)
    store.put(item)
    path = store.root / item.session_id / f"{item.edit_id}.json"
    payload = json.loads(path.read_bytes())
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageError, match="损坏|格式"):
        store.load(item.session_id, item.edit_id)
    assert not item.path.exists()


def test_existing_empty_file_checkpoint_keeps_v1_and_cannot_replace_absence(tmp_path):
    item = creation(
        tmp_path,
        original=b"",
        before_version=hashlib.sha256(b"").hexdigest(),
        target_version=hashlib.sha256(b"new").hexdigest(),
        operation="replace",
    )
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=10)
    store.put(item)
    payload = json.loads((store.root / item.session_id / f"{item.edit_id}.json").read_bytes())
    assert payload["version"] == 1 and "operation" not in payload and payload["original"] == ""
    assert store.load(item.session_id, item.edit_id) == item
    with pytest.raises(StorageError, match="拒绝覆盖"):
        store.put(creation(tmp_path))


def test_creation_cannot_accidentally_use_restore_existing_file(tmp_path):
    item = creation(tmp_path)
    item.path.write_bytes(b"")
    with pytest.raises(ToolError, match="原目标不存在"):
        plan_restore(read_snapshot(item.path, 10), item)
    assert item.path.read_bytes() == b""


def test_create_ledger_keeps_missing_version_and_unknown_until_finish(tmp_path):
    item = creation(tmp_path)
    link = {
        "turn_id": item.turn_id,
        "request_id": item.request_id,
        "tool_call_id": item.tool_call_id,
        "edit_id": item.edit_id,
    }
    log = EditLog()
    log.apply(
        "edit_planned",
        {
            **link,
            "path": str(item.path),
            "before_version": None,
            "target_version": item.target_version,
            "diff": "new empty file",
            "operation": "create",
        },
    )
    log.apply("edit_started", link)
    log.recover()
    assert log.operations[item.edit_id]["status"] == "unknown"
    assert log.operations[item.edit_id]["before_version"] is None
    result = {
        **link,
        "status": "applied",
        "after_version": item.target_version,
        "duration_ms": 1,
        "error": None,
    }
    with pytest.raises(ValueError, match="operation changed"):
        log.apply("edit_finished", {**result, "operation": "replace"})
    assert log.operations[item.edit_id]["status"] == "unknown"
    log.apply("edit_finished", result)
    undo = UndoLog()
    undo.apply(
        "undo_requested",
        {
            "undo_id": "d" * 32,
            "edit_id": item.edit_id,
            "path": str(item.path),
            "before_version": item.target_version,
            "target_version": None,
            "source_turn_id": item.turn_id,
            "source_request_id": item.request_id,
            "source_tool_call_id": item.tool_call_id,
        },
        log.operations,
        {},
    )
    assert undo.operations["d" * 32]["target_version"] is None
    assert not item.path.exists()


def test_replace_tool_cannot_claim_missing_original_in_creation_event(tmp_path):
    with SessionStore(tmp_path / "sessions") as store:
        session, link, _, _ = planned_edit(store, tmp_path / "old.txt", b"old")
        events = store.load_events(session)
        # 新的独立工具来源仍标成replace_text，用来验证SessionLog拒绝伪造创建类型。
        source = {k: v for k, v in link.items() if k != "edit_id"}
        source["tool_call_id"] = "different-call"
        store.append_tool_event(
            session, "tool_started", {**source, "name": "replace_text", "arguments": "{}"}
        )
        with pytest.raises(ValueError, match="operation does not match"):
            store.append_edit_event(
                session,
                "edit_planned",
                {
                    **source,
                    "edit_id": "f" * 32,
                    "path": str(tmp_path / "new.txt"),
                    "operation": "create",
                    "before_version": None,
                    "target_version": hashlib.sha256(b"").hexdigest(),
                    "diff": "new empty file",
                },
            )
        assert len(store.load_events(session)) == len(events) + 1
        assert not (tmp_path / "new.txt").exists()


def test_failed_snapshot_publication_does_not_create_target_or_publish_record(
    tmp_path, monkeypatch
):
    import adam.session.checkpoints as checkpoints

    item = creation(tmp_path)
    store = CheckpointStore(tmp_path / "snapshots", max_bytes=10)

    def fail_publish(*_args):
        raise OSError("injected snapshot publication failure")

    monkeypatch.setattr(checkpoints.os, "replace", fail_publish)
    with pytest.raises(StorageError, match="检查点文件操作失败"):
        store.put(item)
    assert not item.path.exists()
    assert not list(store.root.rglob("*.json")) and not list(store.root.rglob("*.tmp"))

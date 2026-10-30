"""T7.1：显式检查点 API 使用真实文件和 JSONL；编辑工具自动接入留在 T7.2。"""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import adam.session.checkpoints as checkpoint_module
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import StorageError
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy


def planned_edit(store, target, raw):
    """建立真实父请求、工具与编辑计划，原字节来自同一已知文件。"""
    target.write_bytes(raw)
    editor = EditTools(PathPolicy(target.parent, PermissionConfig()), ToolLimits())
    plan = editor.prepare(
        {
            "path": str(target),
            "old_text": "old",
            "new_text": "new",
            "expected_version": hashlib.sha256(raw).hexdigest(),
        }
    )
    session_id = store.create_session("checkpoint", "")
    store.select_session(session_id)
    turn_id = store.start_turn(session_id, "change old to new")
    request_id = store.start_request(session_id, turn_id, "fake")
    store.finish_request(session_id, turn_id, request_id, (5, 2))
    source = {"turn_id": turn_id, "request_id": request_id, "tool_call_id": "edit-call"}
    store.append_tool_event(
        session_id,
        "tool_started",
        {
            **source,
            "name": "replace_text",
            "arguments": "{}",
        },
    )
    link = {**source, "edit_id": "e" * 32}
    store.append_edit_event(
        session_id,
        "edit_planned",
        {
            **link,
            "path": str(target),
            "before_version": plan.before_version,
            "target_version": hashlib.sha256(plan.candidate).hexdigest(),
            "diff": plan.diff,
        },
    )
    return session_id, link, editor, plan


def finish_edit(store, session_id, link, status, candidate):
    store.append_edit_event(
        session_id,
        "edit_finished",
        {
            **link,
            "status": status,
            "duration_ms": 1,
            "after_version": hashlib.sha256(candidate).hexdigest() if status == "applied" else None,
            "error": None if status in {"applied", "denied"} else "injected editing failure",
        },
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"old\nlast",
        b"old\r\nlast\r\n",
        b"\xef\xbb\xbfold\r\n" + "中文".encode(),
    ],
)
def test_original_bytes_and_sources_survive_edit_and_restart(tmp_path, monkeypatch, raw):
    target = tmp_path / "source.txt"
    with SessionStore(tmp_path / "sessions") as store:
        session_id, link, editor, plan = planned_edit(store, target, raw)
        store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        assert target.read_bytes() == raw
        with pytest.raises(StorageError, match="未确认成功"):
            store.load_edit_checkpoint(session_id, link["edit_id"], max_bytes=1024)
        store.append_edit_event(session_id, "edit_started", link)
        editor.apply(plan)
        finish_edit(store, session_id, link, "applied", plan.candidate)

    real_open = Path.open

    def no_target_access(path, *args, **kwargs):
        if path == target:
            raise AssertionError("loading a checkpoint must not open its target")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", no_target_access)
        with SessionStore(tmp_path / "sessions") as reopened:
            value = reopened.load_edit_checkpoint(session_id, link["edit_id"], max_bytes=1024)
            assert value.original == raw and value.before_version == hashlib.sha256(raw).hexdigest()
            assert value.target_version == hashlib.sha256(plan.candidate).hexdigest()
            assert (value.turn_id, value.request_id, value.tool_call_id) == (
                link["turn_id"],
                link["request_id"],
                link["tool_call_id"],
            )
            assert reopened.load_messages(session_id) == []
            assert reopened.load_edits(session_id)[0]["status"] == "applied"
    assert target.read_bytes() == plan.candidate


@pytest.mark.parametrize("state", ["planned", "unknown", "failed", "conflict", "denied"])
def test_snapshot_never_upgrades_unconfirmed_or_failed_edit(tmp_path, state):
    raw, target = b"old\n", tmp_path / "source.txt"
    with SessionStore(tmp_path / "sessions") as store:
        session_id, link, editor, plan = planned_edit(store, target, raw)
        store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        if state not in {"planned", "denied"}:
            store.append_edit_event(session_id, "edit_started", link)
        if state == "unknown":
            # 文件确已改变但成功记录缺失，重启也不能以当前内容猜测执行结果。
            editor.apply(plan)
        elif state in {"failed", "conflict", "denied"}:
            finish_edit(store, session_id, link, state, plan.candidate)
    with SessionStore(tmp_path / "sessions") as reopened:
        with pytest.raises(StorageError, match="未确认成功"):
            reopened.load_edit_checkpoint(session_id, link["edit_id"], max_bytes=1024)
        assert reopened.load_edits(session_id)[0]["status"] == (
            "not_started" if state == "planned" else state
        )
        assert reopened.load_messages(session_id) == []
    assert target.read_bytes() == (plan.candidate if state == "unknown" else raw)


@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
def test_save_failure_preserves_target_and_does_not_publish(tmp_path, monkeypatch, operation):
    """故障注入快照写入、同步或发布边界；只检查本阶段的显式保存契约。"""
    raw, target = b"old", tmp_path / "source.txt"
    with SessionStore(tmp_path / "sessions") as store:
        session_id, link, _, _ = planned_edit(store, target, raw)
        original_temp = checkpoint_module.tempfile.NamedTemporaryFile

        def fail(*args):
            raise OSError("injected checkpoint IO failure")

        @contextmanager
        def failing_file(*args, **kwargs):
            with original_temp(*args, **kwargs) as file:
                yield SimpleNamespace(
                    name=file.name,
                    write=fail,
                    flush=file.flush,
                    fileno=file.fileno,
                )

        with monkeypatch.context() as patch:
            if operation == "write":
                patch.setattr(checkpoint_module.tempfile, "NamedTemporaryFile", failing_file)
            else:
                patch.setattr(checkpoint_module.os, operation, fail)
            with pytest.raises(StorageError, match="检查点文件操作失败"):
                store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        assert target.read_bytes() == raw
        assert store.load_edits(session_id)[0]["status"] == "planned"
        root = store.root / "_checkpoints"
        assert not list(root.rglob("*.json")) and not list(root.rglob("*.tmp"))
        store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        assert len(list(root.rglob("*.json"))) == 1


@pytest.mark.parametrize(
    "field", ["turn_id", "request_id", "tool_call_id", "path", "target_version"]
)
def test_applied_checkpoint_must_match_original_ledger(tmp_path, field):
    raw, target = b"old", tmp_path / "source.txt"
    with SessionStore(tmp_path / "sessions") as store:
        session_id, link, editor, plan = planned_edit(store, target, raw)
        store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        store.append_edit_event(session_id, "edit_started", link)
        editor.apply(plan)
        finish_edit(store, session_id, link, "applied", plan.candidate)
        path = store.root / "_checkpoints" / session_id / f"{link['edit_id']}.json"
        value = json.loads(path.read_bytes())
        value[field] = (
            str(tmp_path / "other.txt")
            if field == "path"
            else "f" * (64 if field == "target_version" else 32)
        )
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(StorageError, match="不一致"):
            store.load_edit_checkpoint(session_id, link["edit_id"], max_bytes=1024)
        assert target.read_bytes() == plan.candidate


def test_late_or_mismatched_snapshot_cannot_replace_pre_edit_evidence(tmp_path):
    raw, target = b"old", tmp_path / "source.txt"
    with SessionStore(tmp_path / "sessions") as store:
        session_id, link, editor, plan = planned_edit(store, target, raw)
        with pytest.raises(ValueError, match="bytes"):
            store.save_edit_checkpoint(session_id, link["edit_id"], b"wrong", max_bytes=1024)
        store.append_edit_event(session_id, "edit_started", link)
        with pytest.raises(StorageError, match="待执行计划"):
            store.save_edit_checkpoint(session_id, link["edit_id"], raw, max_bytes=1024)
        editor.apply(plan)
        finish_edit(store, session_id, link, "applied", plan.candidate)
        with pytest.raises(StorageError):
            store.load_edit_checkpoint(session_id, link["edit_id"], max_bytes=1024)
        assert not list((store.root / "_checkpoints").rglob("*.json"))
        assert target.read_bytes() == plan.candidate

import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

from adam.contracts.errors import SessionNotFoundError, StorageError
from adam.session.outcome import ExecutionOutcome
from adam.session.storage import SessionStore


def test_roundtrip_layout_and_isolation(tmp_path):
    with SessionStore(tmp_path) as store:
        first = store.create_session("学习", "system")
        second = store.create_session("other", "")
        store.append_turn(first, "中文\n第二行", "answer")
        store.append_turn(second, "other", "reply")
        store.append_turn(first, "next", "final")
    path = tmp_path / datetime.now().strftime("%Y/%m/%d") / f"{first}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(first) == 8
    assert len(records) == 9
    assert records[0]["type"] == "session_meta"
    assert all(set(r) == {"type", "timestamp", "payload"} for r in records)
    assert records[1]["payload"]["turn_id"] == records[2]["payload"]["turn_id"]
    with SessionStore(tmp_path) as store:
        assert store.get_session(first).system_prompt == "system"
        assert [m.content for m in store.load_messages(first)] == [
            "中文\n第二行",
            "answer",
            "next",
            "final",
        ]
        assert [m.position for m in store.load_messages(first)] == [0, 1, 2, 3]
        assert len(store.load_messages(second)) == 2


@pytest.mark.parametrize("suffix", [b"{", b"\n", b'{"type":"message"}\n'])
def test_corrupt_tail_is_not_overwritten(tmp_path, suffix):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("test", "")
        store.append_turn(session_id, "q", "a")
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        with path.open("ab") as file:
            file.write(suffix)
        original = path.read_bytes()
        with pytest.raises(StorageError, match="不完整"):
            store.load_messages(session_id)
        with pytest.raises(StorageError):
            store.append_turn(session_id, "next", "reply")
        assert path.read_bytes() == original


def test_half_turn_and_invalid_metadata(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("test", "")
        store.append_turn(session_id, "q", "a")
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        lines = path.read_bytes().splitlines(keepends=True)
        # 保留 turn_started 和首条 message，模拟完整消息块尚未提交。
        path.write_bytes(b"".join(lines[:3]))
        with pytest.raises(StorageError):
            store.load_messages(session_id)
        meta = json.loads(lines[0])
        meta["payload"]["version"] = 99
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        with pytest.raises(StorageError):
            store.get_session(session_id)


def test_save_failure_rolls_back(tmp_path, monkeypatch):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("test", "")
        store.append_turn(session_id, "old", "answer")
        original = store.load_messages(session_id)
        real_fsync = os.fsync
        calls = 0

        def fail_once(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_once)
        with pytest.raises(StorageError, match="会话文件操作失败") as caught:
            store.append_turn(session_id, "not saved", "reply")
        assert isinstance(caught.value.__cause__, OSError)
        assert str(caught.value.__cause__) == "injected"
        assert "injected" not in caught.value.user_message
        assert store.load_messages(session_id) == original
        store.append_turn(session_id, "retry", "ok")
        assert len(store.load_messages(session_id)) == 4


def test_lock_switch_and_process_release(tmp_path):
    with SessionStore(tmp_path) as first, SessionStore(tmp_path) as second:
        a = first.create_session("a", "")
        b = first.create_session("b", "")
        first.select_session(a)
        with pytest.raises(StorageError, match="占用"):
            second.select_session(a)
        second.select_session(b)
        with pytest.raises(StorageError):
            first.select_session(b)
        with pytest.raises(StorageError):
            second.select_session(a)  # 切换失败仍持有旧锁。
        first.close()
        second.select_session(a)
        first.select_session(b)
    code = """import sys,time
from pathlib import Path
from adam.session.storage import SessionStore
store=SessionStore(Path(sys.argv[1]))
store.select_session(sys.argv[2])
print('locked', flush=True)
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), a], stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with SessionStore(tmp_path) as store, pytest.raises(StorageError):
            store.select_session(a)
    finally:
        process.kill()
        process.communicate(timeout=5)
    with SessionStore(tmp_path) as store:
        store.select_session(a)  # 遗留锁文件不会阻止进程退出后的恢复。


def test_id_collision_and_missing_session(tmp_path, monkeypatch):
    import adam.session.storage as storage

    ids = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(storage.secrets, "token_hex", lambda _: next(ids))
    with SessionStore(tmp_path) as store:
        assert store.create_session("a", "") == "aaaaaaaa"
        assert store.create_session("b", "") == "bbbbbbbb"
        for session_id in ["ffffffff", "../bad", "1"]:
            with pytest.raises(SessionNotFoundError):
                store.select_session(session_id)


@pytest.mark.parametrize(
    "action",
    [
        "duplicate_request",
        "duplicate_turn",
        "success_without_messages",
        "complete_failed_request",
        "request_after_finished_turn",
        "tool_after_failed_request",
    ],
)
def test_invalid_state_transition_keeps_log_unchanged(tmp_path, action):
    """终态不可重复或逆转，成功轮次和工具调用不能来自失败的模型请求。"""
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("states", "")
        turn_id = store.start_turn(session_id, "question")
        request_id = store.start_request(session_id, turn_id, "test")
        store.finish_request(
            session_id, turn_id, request_id, (1, 2), outcome=ExecutionOutcome.failure(ValueError())
        )
        if action in {"duplicate_turn", "request_after_finished_turn"}:
            store.finish_turn(session_id, turn_id, ExecutionOutcome.failure(ValueError()))
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        original = path.read_bytes()
        with pytest.raises(ValueError):
            if action == "duplicate_request":
                store.finish_request(session_id, turn_id, request_id, (1, 2))
            elif action == "duplicate_turn":
                store.finish_turn(
                    session_id, turn_id, ExecutionOutcome.failure(KeyboardInterrupt())
                )
            elif action == "success_without_messages":
                store.finish_turn(session_id, turn_id, ExecutionOutcome.success())
            elif action == "request_after_finished_turn":
                store.start_request(session_id, turn_id, "test")
            elif action == "tool_after_failed_request":
                store.append_tool_event(
                    session_id,
                    "tool_started",
                    {
                        "turn_id": turn_id,
                        "request_id": request_id,
                        "tool_call_id": "file1",
                        "name": "file_info",
                        "arguments": '{"path":"."}',
                    },
                )
            else:
                store.append_messages(
                    session_id,
                    turn_id,
                    [
                        {"role": "user", "content": "question"},
                        {"role": "assistant", "content": "done"},
                    ],
                )
        assert path.read_bytes() == original
        assert store.usage(session_id) == (3, False)
        assert store.load_messages(session_id) == []


@pytest.mark.parametrize(
    "details",
    [
        {"reason": "completed", "duration_ms": -1, "error": None},
        {"reason": "completed", "duration_ms": True, "error": None},
        {"reason": "completed", "duration_ms": float("nan"), "error": None},
        {
            "reason": "completed",
            "duration_ms": 1,
            "error": {"message": "error", "http_status": None},
        },
        {"reason": "api_http", "duration_ms": 1, "error": {"message": "error", "http_status": 503}},
        {"reason": "completed"},
    ],
)
def test_invalid_outcome_details_are_rejected(tmp_path, details):
    """真实文件中的不合法耗时、成功携带错误及半套详情必须拒绝读取。"""
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("outcome", "")
        store.append_turn(session_id, "q", "a")
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        events = store.load_events(session_id)
        events[-1]["payload"] = {"turn_id": events[-1]["payload"]["turn_id"], **details}
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        with pytest.raises(StorageError):
            store.load_messages(session_id)


def test_earlier_v4_missing_details_remain_unknown(tmp_path):
    """早期 v4 没有原因与耗时，回放仍读状态，不伪造新字段。"""
    from adam.session.log import parse_log

    with SessionStore(tmp_path) as store:
        session_id = store.create_session("v4", "")
        store.append_turn(session_id, "q", "a")
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        events = store.load_events(session_id)
        events[-1]["payload"] = {"turn_id": events[-1]["payload"]["turn_id"]}
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        assert len(store.load_messages(session_id)) == 2
        log = parse_log(path.read_bytes(), session_id)
        turn = next(iter(log.turns.values()))
        assert turn["status"] == "succeeded"
        assert turn["reason"] is None and turn["duration_ms"] is None

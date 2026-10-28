import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

from adam.storage import SessionNotFoundError, SessionStore, StorageError


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
        with pytest.raises(OSError, match="injected"):
            store.append_turn(session_id, "not saved", "reply")
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
from adam.storage import SessionStore
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
    import adam.storage as storage

    ids = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(storage.secrets, "token_hex", lambda _: next(ids))
    with SessionStore(tmp_path) as store:
        assert store.create_session("a", "") == "aaaaaaaa"
        assert store.create_session("b", "") == "bbbbbbbb"
        for session_id in ["ffffffff", "../bad", "1"]:
            with pytest.raises(SessionNotFoundError):
                store.select_session(session_id)

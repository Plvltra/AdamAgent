"""重启恢复在取得新写锁后执行；本文件注入遗留记录，真实强杀另行验收。"""

import os

import httpx
import pytest

from adam.config import LLMConfig
from adam.diagnostics import read_report
from adam.errors import BudgetError
from adam.llm import LLMClient, LLMError
from adam.outcome import ExecutionOutcome
from adam.storage import SessionStore, StorageError, record
from tests.support import build_conversation


@pytest.mark.parametrize("phase", ["request", "known_usage", "tool", "command", "failed_turn"])
def test_reopen_closes_only_unfinished_operations(tmp_path, phase):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("recovery", "")
        store.append_turn(session_id, "old", "saved")
        turn_id = store.start_turn(session_id, "unfinished")
        request_id = store.start_request(session_id, turn_id, "test")
        if phase in {"tool", "command"}:
            store.finish_request(session_id, turn_id, request_id, (7, 3))
            link = {"turn_id": turn_id, "request_id": request_id, "tool_call_id": "t"}
            store.append_tool_event(
                session_id,
                "tool_started",
                {
                    **link,
                    "name": "run_command" if phase == "command" else "file_info",
                    "arguments": "{}",
                },
            )
            if phase == "command":
                store.append_command_event(
                    session_id,
                    "command_started",
                    {
                        **link,
                        "execution_id": "exec1",
                        "argv": ["unused"],
                        "cwd": str(tmp_path),
                    },
                )
        if phase == "failed_turn":
            store.finish_turn(session_id, turn_id, ExecutionOutcome.failure(LLMError("failed")))
    path = next(tmp_path.glob("*/*/*/*.jsonl"))
    if phase == "known_usage":
        # 模拟用量行完整落盘、紧随其后的 request_finished 尚未写入。
        with path.open("ab") as file:
            file.write(
                record(
                    "request_usage",
                    {
                        "turn_id": turn_id,
                        "request_id": request_id,
                        "input_tokens": 7,
                        "output_tokens": 3,
                    },
                )
            )
    original = path.read_bytes()
    requests = []
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r))) as http,
        SessionStore(tmp_path) as store,
    ):
        conversation = build_conversation(
            store, LLMClient(LLMConfig("https://example.test", "test", "x"), http), ""
        )
        conversation.resume(session_id)
        report = read_report(path)
        turn = report["turns"][-1]
        assert turn["status"] == ("failed" if phase == "failed_turn" else "interrupted")
        assert turn["reason"] == ("api_error" if phase == "failed_turn" else "restart_recovery")
        request = turn["requests"][0]
        assert request["status"] == ("succeeded" if phase in {"tool", "command"} else "interrupted")
        if request["status"] == "interrupted":
            assert request["duration_ms"] is None
        assert report["known_tokens"] == (10 if phase in {"known_usage", "tool", "command"} else 0)
        assert report["unknown_usage"] == (phase in {"request", "failed_turn"})
        assert [m.content for m in store.load_messages(session_id)] == ["old", "saved"]
        if phase in {"tool", "command"}:
            assert turn["tools"][0]["status"] == "interrupted"
            assert turn["tools"][0]["finished"] is False
        if phase == "command":
            assert turn["commands"][0]["status"] == "unknown"
            with pytest.raises(StorageError, match="结果未知"):
                conversation.send("do not replay")
        elif report["unknown_usage"]:
            with pytest.raises(BudgetError, match="未知"):
                conversation.send("do not resend")
    assert requests == []
    assert path.read_bytes().startswith(original)
    assert [p.read_bytes() for p in path.parent.glob("*.bak")] == [original]
    stable = path.read_bytes()
    with SessionStore(tmp_path) as store:
        store.select_session(session_id)
    assert path.read_bytes() == stable
    assert len(list(path.parent.glob("*.bak"))) == 1


@pytest.mark.parametrize("tail", ["json", "utf8", "newline", "user", "answer"])
def test_tail_recovery_preserves_complete_lines_and_committed_history(tmp_path, tail):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("tail", "")
        store.append_turn(session_id, "old", "saved")
        turn_id = store.start_turn(session_id, "unfinished")
    path = next(tmp_path.glob("*/*/*/*.jsonl"))
    complete = path.read_bytes()
    if tail in {"user", "answer"}:
        complete += record(
            "message", {"turn_id": turn_id, "message": {"role": "user", "content": "unfinished"}}
        )
        if tail == "answer":
            complete += record(
                "message",
                {"turn_id": turn_id, "message": {"role": "assistant", "content": "uncommitted"}},
            )
    original = complete
    if tail == "json":
        original += b'{"type":'
    elif tail == "utf8":
        original += b'{"text":"\xe4\xb8'
    elif tail == "newline":
        original = complete[:-1]
    path.write_bytes(original)
    with SessionStore(tmp_path) as store:
        store.select_session(session_id)
        assert [m.content for m in store.load_messages(session_id)] == ["old", "saved"]
        report = read_report(path)
        assert report["turns"][-1]["status"] == "interrupted"
        recovery = report["recoveries"][0]["payload"]
        assert recovery["tail_repaired"] == (tail in {"json", "utf8", "newline"})
        assert recovery["uncommitted_messages"] == {"user": 1, "answer": 2}.get(tail, 0)
        assert path.read_bytes().startswith(complete)
        assert [p.read_bytes() for p in path.parent.glob("*.bak")] == [original]
        store.append_turn(session_id, "fresh", "reply")
        assert [m.content for m in store.load_messages(session_id)] == [
            "old",
            "saved",
            "fresh",
            "reply",
        ]


@pytest.mark.parametrize("corruption", ["middle", "complete_invalid_tail", "invalid_message"])
def test_recovery_rejects_corruption_without_discarding_records(tmp_path, corruption):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("bad", "")
        turn_id = store.start_turn(session_id, "input")
    path = next(tmp_path.glob("*/*/*/*.jsonl"))
    raw = path.read_bytes()
    if corruption == "middle":
        lines = raw.splitlines(keepends=True)
        raw = lines[0] + b"bad\n" + b"".join(lines[1:]) + b"{"
    elif corruption == "complete_invalid_tail":
        raw += b"{}"  # JSON 完整但事件非法，不能当作残缺尾行删除。
    else:
        raw += record(
            "message", {"turn_id": turn_id, "message": {"role": "user", "content": "wrong"}}
        )
    path.write_bytes(raw)
    with SessionStore(tmp_path) as store, pytest.raises(StorageError):
        store.select_session(session_id)
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.bak"))


@pytest.mark.parametrize("failure", ["backup", "temporary", "replace"])
def test_recovery_io_failure_preserves_original_and_previous_lock(tmp_path, monkeypatch, failure):
    with SessionStore(tmp_path) as initial:
        old_id = initial.create_session("old", "")
        target_id = initial.create_session("target", "")
        initial.start_turn(target_id, "unfinished")
    path = next(tmp_path.rglob(target_id + ".jsonl"))
    original = path.read_bytes()
    with SessionStore(tmp_path) as store, SessionStore(tmp_path) as contender:
        store.select_session(old_id)
        with monkeypatch.context() as patch:
            real_fsync = os.fsync
            calls = 0

            def fail_fsync(fd):
                nonlocal calls
                calls += 1
                if calls == (1 if failure == "backup" else 2):
                    raise OSError("private disk detail")
                real_fsync(fd)

            def fail_replace(*args):
                raise OSError("private replace detail")

            if failure == "replace":
                patch.setattr(os, "replace", fail_replace)
            else:
                patch.setattr(os, "fsync", fail_fsync)
            with pytest.raises(StorageError) as caught:
                store.select_session(target_id)
            assert "private" not in str(caught.value)
        assert path.read_bytes() == original
        with pytest.raises(StorageError, match="占用"):
            contender.select_session(old_id)
        store.select_session(target_id)
        assert read_report(path)["turns"][0]["status"] == "interrupted"


def test_locked_session_cannot_be_repaired_by_another_writer(tmp_path):
    with SessionStore(tmp_path) as owner, SessionStore(tmp_path) as contender:
        session_id = owner.create_session("locked", "")
        owner.start_turn(session_id, "active")
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        original = path.read_bytes() + b"{"
        path.write_bytes(original)
        with pytest.raises(StorageError, match="占用"):
            contender.select_session(session_id)
        assert path.read_bytes() == original
        assert not list(path.parent.glob("*.bak"))
        owner.close()
        contender.select_session(session_id)
        assert read_report(path)["turns"][0]["status"] == "interrupted"

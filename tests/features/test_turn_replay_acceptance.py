"""T9.3：真实主流程的失败、流式和缺失轨迹；完整失败可以一致，缺失不能冒充成功。"""

import json
import os
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import AgentConfig
from adam.contracts.errors import LLMError, RequestInterrupted, TurnSaveError
from adam.recording.journal import read_recording
from adam.recording.runtime import replay_turn
from adam.recordings import main
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def state(root):
    """比较业务状态和有效历史，不用新时钟测量值判断是否一致。"""
    with SessionStore(root) as store:
        sid = store.list_sessions()[0].session_id
        store.select_session(sid)
        return {
            "usage": store.usage(sid),
            "history": [message.raw for message in store.load_messages(sid)],
            "ends": [
                (item["type"], item["payload"]["status"], item["payload"]["reason"])
                for item in store.load_events(sid)
                if item["type"] in {"request_finished", "turn_finished", "turn_completed"}
            ],
        }


@pytest.mark.parametrize(
    "failure,code,attempts,usage",
    [
        ("http", "api_http", 1, (0, True)),
        ("not_sent", "api_not_sent", 3, (0, False)),
        ("timeout", "api_timeout", 1, (0, True)),
        ("malformed", "invalid_response", 1, (0, True)),
        ("missing_usage", "usage_unknown", 1, (0, True)),
        ("stream_interrupt", "interrupted", 1, (0, True)),
        ("known_interrupt", "interrupted", 1, (13, False)),
    ],
)
def test_complete_failed_turn_replays_same_state_without_claiming_business_success(
    tmp_path, monkeypatch, failure, code, attempts, usage
):
    streaming = failure in {"stream_interrupt", "known_interrupt"}
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=streaming))

    def respond(body, number):
        if failure == "http":
            return httpx.Response(503, text="remote private error")
        if failure == "not_sent":
            raise httpx.ConnectError("private connection detail")
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout detail")
        if failure == "malformed":
            return httpx.Response(200, content=b"not-json")
        if failure == "missing_usage":
            value = answer("not safe to commit")
            value.pop("usage")
            return httpx.Response(200, json=value)
        parts = [event({"role": "assistant", "content": "partial"})]
        if failure == "known_interrupt":
            parts.append(event(reason="stop", usage=USAGE))
        return sse([*parts, KeyboardInterrupt()])

    shown = []
    with captured_app(tmp_path, respond, config=config) as (app, recorder, _, source, bodies):
        with pytest.raises(RequestInterrupted if streaming else LLMError):
            recorder.run(lambda: app.send("task", on_output=shown.append))
        assert len(bodies) == attempts
        assert [item.content for item in app.history()] == ["old question", "old answer"]
    recording = read_recording(source)
    assert recording.complete
    assert recording.events[-1]["data"]["error"]["code"] == code
    expected = state(config.sessions_dir)
    assert expected["usage"] == usage
    original = source.read_bytes()
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    assert report["status"] == "matched", report
    assert report["reply"] is None and report["error"]["code"] == code
    assert report["output"] == [{"kind": item.kind, "text": item.text} for item in shown]
    assert state(tmp_path / "offline") == expected
    assert source.read_bytes() == original


def test_streamed_tool_arguments_and_unicode_reply_keep_their_boundaries(tmp_path, monkeypatch):
    (tmp_path / "evidence.txt").write_text("evidence=42", encoding="utf-8")
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=True))

    def respond(body, number):
        if number == 1:
            return sse(
                [
                    event(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path":"'},
                                }
                            ]
                        }
                    ),
                    event(
                        {"tool_calls": [{"index": 0, "function": {"arguments": 'evidence.txt"}'}}]}
                    ),
                    event(reason="tool_calls", usage=USAGE),
                    DONE,
                ]
            )
        assert body["messages"][-1]["tool_call_id"] == "read"
        return sse(
            [
                event({"content": "找到"}),
                event({"content": "四十二"}),
                event(reason="stop", usage=USAGE),
                DONE,
            ]
        )

    shown = []
    with captured_app(tmp_path, respond, config=config) as (app, recorder, _, source, bodies):
        assert recorder.run(lambda: app.send("task", on_output=shown.append)) == "找到四十二"
        assert len(bodies) == 2
    expected = state(config.sessions_dir)
    (tmp_path / "evidence.txt").unlink()
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    assert report["status"] == "matched", report
    assert report["output"] == [{"kind": item.kind, "text": item.text} for item in shown]
    assert report["reply"] == "找到四十二"
    assert state(tmp_path / "offline") == expected


def command():
    """真实动作留下计数，离线阶段重复运行一次就会被发现。"""
    return [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('executions.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
    ]


def command_then_answer(body, number):
    value = (
        tool_response([call("command", "run_command", argv=command())])
        if number == 1
        else answer("command done")
    )
    return httpx.Response(200, json=value)


@pytest.mark.parametrize(
    "cut",
    [
        "model_started",
        "model_finished",
        "tools_started",
        "tools_finished",
        "before_finish",
        "partial_finish",
    ],
)
def test_valid_prefix_never_becomes_a_matched_replay_or_repeats_command(tmp_path, monkeypatch, cut):
    with captured_app(tmp_path, command_then_answer) as (app, recorder, _, source, _):
        recorder.run(lambda: app.send("task"))
    original = source.read_bytes()
    lines = original.splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    starts = {
        item["data"]["name"]: item["seq"]
        for item in reversed(records)
        if item["type"] == "call_started"
    }
    if cut in {"before_finish", "partial_finish"}:
        end = len(lines) - 1
    else:
        name, phase = cut.split("_")
        call_id = starts[name]
        end = (
            call_id + 1
            if phase == "started"
            else next(
                item["seq"] + 1
                for item in records
                if item["type"] == "call_finished" and item["data"]["call_id"] == call_id
            )
        )
    prefix = tmp_path / "prefix.jsonl"
    prefix.write_bytes(
        b"".join(lines[:end])
        + (lines[-1][: len(lines[-1]) // 2] if cut == "partial_finish" else b"")
    )
    assert not read_recording(prefix).complete
    before = prefix.read_bytes()
    original_log = next(config_at(tmp_path).sessions_dir.glob("*/*/*/*.jsonl"))
    log_bytes = original_log.read_bytes()
    forbid_external_actions(monkeypatch)
    report = replay_turn(prefix, tmp_path / "offline")
    assert report["status"] == "incomplete", report
    assert (tmp_path / "executions.txt").read_text() == "x"
    assert source.read_bytes() == original and prefix.read_bytes() == before
    assert original_log.read_bytes() == log_bytes


def test_changed_request_count_stops_real_loop_and_keeps_first_difference(tmp_path, monkeypatch):
    with captured_app(tmp_path, command_then_answer) as (app, recorder, _, source, _):
        recorder.run(lambda: app.send("task"))
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline", agent=AgentConfig(max_model_requests=1))
    assert report["status"] == "different", report
    assert report["actual_error"]["code"] == "request_limit"
    assert (tmp_path / "executions.txt").read_text() == "x"
    events = [
        json.loads(line)
        for line in next((tmp_path / "offline").glob("*/*/*/*.jsonl")).read_bytes().splitlines()
    ]
    assert sum(item["type"] == "request_started" for item in events) == 1
    assert not any(
        item["type"] == "turn_completed" and item["payload"]["duration_ms"] is not None
        for item in events
    )


def test_answer_save_io_failure_is_preserved_and_not_misreported_as_reproduced(
    tmp_path, monkeypatch
):
    config = config_at(tmp_path)
    started = False

    def respond(body, number):
        nonlocal started
        started = True
        return httpx.Response(200, json=answer("unsaved answer"))

    with captured_app(tmp_path, respond) as (app, recorder, _, source, bodies):
        path = next(config.sessions_dir.glob("*/*/*/*.jsonl"))
        inode = path.stat().st_ino
        real_sync = os.fsync
        writes = 0

        def fail_answer_commit(fd):
            nonlocal writes
            if started and os.fstat(fd).st_ino == inode:
                writes += 1
                # 请求结算已同步；第二次写入是回答提交，真实写入随后由存储回滚。
                if writes == 2:
                    raise OSError("injected answer fsync failure")
            return real_sync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_answer_commit)
            with pytest.raises(TurnSaveError):
                recorder.run(lambda: app.send("task"))
        assert len(bodies) == 1
        assert [item.content for item in app.history()] == ["old question", "old answer"]
    recording = read_recording(source)
    assert recording.complete
    assert recording.events[-1]["data"]["error"]["code"] == "answer_save_failed"
    original = path.read_bytes()
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    # 重放不制造操作系统故障；今天成功提交的尝试必须与原失败轨迹产生明确差异。
    assert report["status"] == "different", report
    assert "session_events" in report["difference"]["path"]
    assert path.read_bytes() == original


def test_invalid_or_too_early_recording_fails_without_overwriting_sources(tmp_path, monkeypatch):
    with captured_app(tmp_path, command_then_answer) as (app, recorder, _, source, _):
        recorder.run(lambda: app.send("task"))
    forbid_external_actions(monkeypatch)
    before = source.read_bytes()
    assert main(["replay", str(source), "--output", str(source.parent)]) == 2
    assert source.read_bytes() == before
    header_only = tmp_path / "header.jsonl"
    header_only.write_bytes(before.splitlines(keepends=True)[0])
    assert main(["replay", str(header_only), "--output", str(tmp_path / "early")]) == 2
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_bytes(before.splitlines(keepends=True)[0] + b"not-json\n" + b"{}\n")
    assert main(["replay", str(invalid), "--output", str(tmp_path / "bad")]) == 2
    assert not (tmp_path / "early").exists() and not (tmp_path / "bad").exists()


def test_first_request_and_failed_tool_remain_linked_when_second_request_fails(
    tmp_path, monkeypatch
):
    def respond(body, number):
        if number == 1:
            return httpx.Response(
                200, json=tool_response([call("missing", "read_file", path="absent.txt")])
            )
        return httpx.Response(503, text="not retained")

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        with pytest.raises(LLMError):
            recorder.run(lambda: app.send("task"))
        assert len(bodies) == 2
        events = store.load_events(app.session.session_id)
        current_turn = next(
            item["payload"]["turn_id"]
            for item in reversed(events)
            if item["type"] == "turn_started"
        )
        requests = [
            item["payload"]
            for item in events
            if item["type"] == "request_finished" and item["payload"]["turn_id"] == current_turn
        ]
        assert [item["status"] for item in requests] == ["succeeded", "failed"]
    expected = state(config_at(tmp_path).sessions_dir)
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    assert report["status"] == "matched" and report["error"]["code"] == "api_http", report
    assert state(tmp_path / "offline") == expected


def test_actual_difference_before_missing_data_is_not_downgraded_to_incomplete(
    tmp_path, monkeypatch
):
    with captured_app(tmp_path, command_then_answer) as (app, recorder, _, source, _):
        recorder.run(lambda: app.send("task"))
    prefix = tmp_path / "prefix.jsonl"
    prefix.write_bytes(b"".join(source.read_bytes().splitlines(keepends=True)[:-1]))
    forbid_external_actions(monkeypatch)
    report = replay_turn(prefix, tmp_path / "offline", agent=AgentConfig(max_output_tokens=1234))
    assert report["status"] == "different", report
    assert report["difference"]["path"] == "$.input.max_tokens"
    assert report["difference"]["actual"] == 1234
    assert (tmp_path / "executions.txt").read_text() == "x"


def test_recorded_edit_does_not_reapply_or_create_new_checkpoint(tmp_path, monkeypatch):
    from tests.features.test_subagents import outputs

    target = tmp_path / "source.txt"
    target.write_text("old value\n", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            value = tool_response([call("read", "read_file", path="source.txt")])
        elif number == 2:
            value = tool_response(
                [
                    call(
                        "edit",
                        "replace_text",
                        path="source.txt",
                        old_text="old",
                        new_text="new",
                        expected_version=outputs(body)[-1]["version"],
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["status"] == "applied"
            value = answer("edited")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "edited"
        assert target.read_text(encoding="utf-8") == "new value\n" and len(bodies) == 3
        expected_edits = store.load_edits(app.session.session_id)
    target.write_text("external change after recording\n", encoding="utf-8")
    before = target.read_bytes()
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    assert report["status"] == "matched", report
    assert target.read_bytes() == before
    with SessionStore(tmp_path / "offline") as store:
        assert store.load_edits(store.list_sessions()[0].session_id) == expected_edits
    assert not (tmp_path / "offline" / "_checkpoints").exists()

"""T9.1：标准应用录制真实文件/命令、流式响应、失败及写入边界，不调用付费 API。"""

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.contracts.errors import ApprovalDeniedError, LLMError, RequestInterrupted
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, RecordingTools, TurnRecorder
from adam.recording.format import RecordingError, RecordingLimits
from adam.recording.journal import RecordingWriter, read_recording
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call
from tests.features.test_task_planning import step
from tests.features.test_tool_loop import answer, tool_response

SECRET = "credential-must-not-be-recorded"


@contextmanager
def recording_app(root, respond, *, stream=False, approved=True, limits=None):
    """模型走真实 HTTP 适配器的模拟传输；业务组装和工具执行不替换。"""
    config = config_at(root)
    config = replace(config, llm=replace(config.llm, api_key=SECRET, stream=stream))
    requests, bodies = [], []

    def handler(request):
        requests.append(request)
        bodies.append(json.loads(request.content))
        return respond(bodies[-1], len(bodies))

    path = root / "turn.recording.jsonl"
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        sid = store.create_session("recorded", config.system_prompt)
        store.select_session(sid)
        store.append_turn(sid, "old question", "old answer")
        seed = capture_seed(config, store, sid, "task", root)
        with RecordingWriter(path, seed, limits or RecordingLimits()) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            app = build_application(
                config,
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                root,
                confirm_command=lambda _: approved,
                confirm_edit=lambda _: approved,
                decorate_tools=lambda original: RecordingTools(original, recorder),
            )
            app.resume(sid)
            try:
                yield app, recorder, store, path, requests, bodies
            finally:
                app.close()


def finished(recording):
    return [item["data"] for item in recording.events if item["type"] == "call_finished"]


def test_complete_recording_retains_actual_inputs_tools_plan_and_session_facts(tmp_path):
    (tmp_path / "note.txt").write_text("evidence=42", encoding="utf-8")
    argv = [sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('once')"]

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call("read", "read_file", path="note.txt"),
                    call("command", "run_command", argv=argv),
                ]
            )
        elif number == 2:
            value = tool_response(
                [call("done", "update_plan", steps=[step("completed", ["read", "command"])])]
            )
        else:
            value = answer("verified 42")
        return httpx.Response(200, json=value)

    with recording_app(tmp_path, respond) as (app, recorder, store, path, requests, bodies):
        assert recorder.run(lambda: app.send("task")) == "verified 42"
        source = store.load_events(app.session.session_id)
        assert len(requests) == 3 and all(
            SECRET in req.headers["Authorization"] for req in requests
        )
        with pytest.raises(RecordingError):
            recorder.run(lambda: pytest.fail("a second turn must not run"))
        saved = path.read_bytes()
        with pytest.raises(RecordingError):
            recorder.observe("context", {})
        assert path.read_bytes() == saved
    recording = read_recording(path)
    assert recording.complete and not recording.torn_tail
    model_inputs = [
        item["data"]["input"]
        for item in recording.events
        if item["type"] == "call_started" and item["data"]["name"] == "model"
    ]
    assert [item["messages"] for item in model_inputs] == [body["messages"] for body in bodies]
    assert [item["tools"] for item in model_inputs] == [body["tools"] for body in bodies]
    assert [item["max_tokens"] for item in model_inputs] == [body["max_tokens"] for body in bodies]
    assert len(finished(recording)) == 5
    collected = recording.seed["session_events"] + [
        entry
        for item in recording.events
        if "session_events" in item["data"]
        for entry in item["data"]["session_events"]
    ]
    assert collected == source
    assert sum(item["type"] == "plan_updated" for item in collected) == 2
    assert sum(item["type"] == "command_finished" for item in collected) == 1
    assert (tmp_path / "ran.txt").read_text() == "once"
    raw = path.read_text(encoding="utf-8")
    assert SECRET not in raw and "Authorization" not in raw and "base_url" not in raw
    assert "provider field" in raw  # 业务协议要求的推理字段仍保留。


def test_failed_tool_is_recorded_as_result_and_can_be_followed_by_final_answer(tmp_path):
    def respond(body, number):
        value = (
            tool_response([call("missing", "read_file", path="absent.txt")])
            if number == 1
            else answer("file absent")
        )
        return httpx.Response(200, json=value)

    with recording_app(tmp_path, respond) as (app, recorder, _, path, _, _):
        assert recorder.run(lambda: app.send("task")) == "file absent"
    recording = read_recording(path)
    tool = finished(recording)[1]
    assert tool["error"] is None and json.loads(tool["value"][0]["content"])["ok"] is False
    assert recording.complete and recording.events[-1]["data"]["error"] is None


def test_http_failure_preserves_original_error_and_excludes_remote_error_body(tmp_path):
    def respond(body, number):
        return httpx.Response(503, text="remote detail " + SECRET)

    with recording_app(tmp_path, respond) as (app, recorder, store, path, requests, _):
        with pytest.raises(LLMError) as raised:
            recorder.run(lambda: app.send("task"))
        assert raised.value.http_status == 503 and len(requests) == 1
        assert [msg.content for msg in app.history()] == ["old question", "old answer"]
        assert store.usage(app.session.session_id)[1] is True
    recording = read_recording(path)
    error = finished(recording)[0]["error"]
    assert {key: value for key, value in error.items() if key != "input_prefix"} == {
        "kind": "llm",
        "code": "api_http",
        "usage": None,
        "http_status": 503,
    }
    assert error["input_prefix"]["sequence"] == 1
    assert error["input_prefix"]["input_bytes"] > 0
    assert error["input_prefix"]["common_prefix_bytes"] is None
    assert recording.complete and recording.events[-1]["data"]["error"] == error
    assert SECRET not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["not_sent", "timeout", "interrupted"])
def test_transport_failures_retain_retry_and_unknown_usage_semantics(tmp_path, failure):
    def respond(body, number):
        if failure == "not_sent":
            raise httpx.ConnectError(SECRET)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        return sse([event({"role": "assistant", "content": "partial"}), KeyboardInterrupt()])

    exception = RequestInterrupted if failure == "interrupted" else LLMError
    with recording_app(tmp_path, respond, stream=failure == "interrupted") as (
        app,
        recorder,
        store,
        path,
        requests,
        _,
    ):
        with pytest.raises(exception):
            recorder.run(lambda: app.send("task"))
        assert len(requests) == (3 if failure == "not_sent" else 1)
        assert store.usage(app.session.session_id) == (
            (0, False) if failure == "not_sent" else (0, True)
        )
    recording = read_recording(path)
    calls = finished(recording)
    assert recording.complete and len(calls) == (3 if failure == "not_sent" else 1)
    if failure == "not_sent":
        assert all(
            item["error"]["kind"] == "not_sent" and item["error"]["usage"] == [0, 0]
            for item in calls
        )
    elif failure == "timeout":
        assert calls[0]["error"]["code"] == "api_timeout"
    else:
        assert calls[0]["error"]["kind"] == "request_interrupted"
        assert any(
            item["type"] == "model_output" and item["data"]["text"] == "partial"
            for item in recording.events
        )
    assert SECRET not in path.read_text(encoding="utf-8")


def test_streamed_text_and_end_boundary_match_display_and_complete_response(tmp_path):
    def respond(body, number):
        return sse(
            [
                event({"role": "assistant", "reasoning_content": "private reasoning"}),
                event({"content": "你"}),
                event({"content": "好"}),
                event(reason="stop", usage=USAGE),
                DONE,
            ]
        )

    output = []
    with recording_app(tmp_path, respond, stream=True) as (app, recorder, _, path, requests, _):
        assert recorder.run(lambda: app.send("task", on_output=output.append)) == "你好"
        assert len(requests) == 1
    recording = read_recording(path)
    observed = [item["data"] for item in recording.events if item["type"] == "model_output"]
    assert [(item["kind"], item["text"]) for item in observed] == [
        (item.kind, item.text) for item in output
    ]
    assert "".join(item["text"] for item in observed) == "你好"
    assert observed[-1]["kind"] == "response_end"
    completion = finished(recording)[0]["value"]
    assert completion["message"]["reasoning_content"] == "private reasoning"
    assert completion["usage"] == [10, 3]


def test_denied_command_keeps_approval_fact_and_never_creates_marker(tmp_path):
    argv = [sys.executable, "-c", "from pathlib import Path; Path('forbidden').touch()"]

    def respond(body, number):
        return httpx.Response(200, json=tool_response([call("command", "run_command", argv=argv)]))

    with recording_app(tmp_path, respond, approved=False) as (app, recorder, _, path, requests, _):
        with pytest.raises(ApprovalDeniedError):
            recorder.run(lambda: app.send("task"))
        assert len(requests) == 1 and not (tmp_path / "forbidden").exists()
    recording = read_recording(path)
    tool = finished(recording)[1]
    assert tool["error"]["code"] == "approval_denied"
    assert any(item["type"] == "command_denied" for item in tool["session_events"])
    assert recording.complete


@pytest.mark.parametrize("write_number", [2, 5])
def test_recording_fsync_failure_stops_without_repeating_real_command(
    tmp_path, monkeypatch, write_number
):
    argv = [sys.executable, "-c", "from pathlib import Path; Path('marker').write_text('once')"]

    def respond(body, number):
        return httpx.Response(200, json=tool_response([call("command", "run_command", argv=argv)]))

    with recording_app(tmp_path, respond) as (app, recorder, store, path, requests, _):
        original = os.fsync
        inode = path.stat().st_ino
        count = 1  # 录制头已同步；只对这份实际文件的后续 fsync 注入失败。

        def fail_recording(fd):
            nonlocal count
            if os.fstat(fd).st_ino == inode:
                count += 1
                if count == write_number:
                    raise OSError("injected recorder fsync failure")
            return original(fd)

        monkeypatch.setattr(os, "fsync", fail_recording)
        with pytest.raises(RecordingError):
            recorder.run(lambda: app.send("task"))
        assert len(requests) == (0 if write_number == 2 else 1)
        assert len(store.load_executions(app.session.session_id)) == (0 if write_number == 2 else 1)
    recording = read_recording(path)
    assert not recording.complete
    assert (tmp_path / "marker").exists() == (write_number == 5)
    if write_number == 5:
        assert (tmp_path / "marker").read_text() == "once"


def test_recording_event_limit_blocks_request_and_existing_file_is_never_replaced(tmp_path):
    with recording_app(
        tmp_path,
        lambda *_: pytest.fail("no request permitted"),
        limits=RecordingLimits(max_events=1),
    ) as (app, recorder, _, path, requests, _):
        before = path.read_bytes()
        with pytest.raises(RecordingError):
            recorder.run(lambda: app.send("task"))
        assert requests == [] and path.read_bytes() == before
    seed = read_recording(path).seed
    with pytest.raises(RecordingError):
        RecordingWriter(path, seed)
    assert path.read_bytes() == before


def test_display_failure_preserves_original_exception_and_records_received_text(tmp_path):
    def respond(body, number):
        return sse(
            [
                event({"role": "assistant", "content": "partial"}),
                event(reason="stop", usage=USAGE),
                DONE,
            ]
        )

    original = RuntimeError(SECRET)

    def display(output):
        if output.kind == "text":
            raise original

    with recording_app(tmp_path, respond, stream=True) as (app, recorder, _, path, requests, _):
        with pytest.raises(RuntimeError) as raised:
            recorder.run(lambda: app.send("task", on_output=display))
        assert raised.value is original and len(requests) == 1
    recording = read_recording(path)
    assert recording.complete and finished(recording)[0]["error"]["kind"] == "internal"
    assert any(
        item["type"] == "model_output" and item["data"]["text"] == "partial"
        for item in recording.events
    )
    assert SECRET not in path.read_text(encoding="utf-8")


def test_recording_write_failure_does_not_mask_existing_model_failure(tmp_path, monkeypatch):
    with recording_app(tmp_path, lambda *_: httpx.Response(503, text=SECRET)) as (
        app,
        recorder,
        _,
        path,
        requests,
        _,
    ):
        original_fsync = os.fsync
        inode = path.stat().st_ino
        count = 1

        def fail_error_record(fd):
            nonlocal count
            if os.fstat(fd).st_ino == inode:
                count += 1
                # 头部及调用开始已落盘；仅在保存模型失败信息时注入同步故障。
                if count == 3:
                    raise OSError("injected error-record sync failure")
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_error_record)
        with pytest.raises(LLMError) as raised:
            recorder.run(lambda: app.send("task"))
        assert raised.value.http_status == 503 and len(requests) == 1
        assert any("录制" in note for note in raised.value.__notes__)
    assert not read_recording(path).complete
    assert SECRET not in path.read_text(encoding="utf-8")

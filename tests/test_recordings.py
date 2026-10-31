"""T9.1：录制协议、损坏输入、资源上限和只读加载边界。"""

import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from adam.contracts.errors import LLMError
from adam.recording.format import RecordingError, RecordingLimits, error_record
from adam.recording.journal import RecordingWriter, read_recording
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_recording import recording_app


@pytest.fixture
def completed_recording(tmp_path):
    with recording_app(tmp_path, lambda *_: httpx.Response(200, json=answer("done"))) as (
        app,
        recorder,
        _,
        path,
        _,
        _,
    ):
        recorder.run(lambda: app.send("task"))
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    return path, events


def rewrite(path, events):
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "boolean_version",
        "sequence",
        "extra_auth",
        "config_secret",
        "bad_budget",
        "wrong_result_id",
        "boolean_usage",
        "bad_message",
        "wrong_answer",
        "wrong_input",
        "second_turn",
        "unmatched_finish",
        "overlapping_call",
    ],
)
def test_invalid_recording_is_rejected_without_changing_file(completed_recording, mutation):
    path, events = completed_recording
    seed = events[0]["data"]["seed"]
    if mutation == "version":
        events[0]["data"]["version"] = 2
    elif mutation == "boolean_version":
        events[0]["data"]["version"] = True
    elif mutation == "sequence":
        events[1]["seq"] = 999
    elif mutation == "extra_auth":
        events[1]["data"]["input"]["Authorization"] = "must never be accepted"
    elif mutation == "config_secret":
        seed["controls"]["agent"]["api_key"] = "secret"
    elif mutation == "bad_budget":
        seed["controls"]["agent"]["max_model_requests"] = False
    elif mutation == "wrong_result_id":
        events[2]["data"]["call_id"] = 55
    elif mutation == "boolean_usage":
        events[2]["data"]["value"]["usage"] = [True, 2]
    elif mutation == "bad_message":
        events[2]["data"]["value"]["message"]["role"] = "user"
    elif mutation == "wrong_answer":
        events[-1]["data"]["reply"] = "not the committed reply"
    elif mutation == "wrong_input":
        seed["user_text"] = "different input"
    elif mutation == "second_turn":
        last = deepcopy(
            next(
                item
                for item in events[2]["data"]["session_events"]
                if item["type"] == "turn_started"
            )
        )
        last["payload"]["turn_id"] = "b" * 32
        events[2]["data"]["session_events"].append(last)
    elif mutation == "unmatched_finish":
        events[1] = {**events[2], "seq": 1}
    elif mutation == "overlapping_call":
        events[2] = deepcopy(events[1])
        events[2]["seq"] = events[2]["data"]["call_id"] = 2
    rewrite(path, events)
    before = path.read_bytes()
    with pytest.raises(RecordingError):
        read_recording(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["duplicate_key", "bad_utf8", "nonfinite", "middle_line", "extra_after_end"]
)
def test_invalid_json_and_middle_damage_are_not_recovered(completed_recording, damage):
    path, events = completed_recording
    raw = path.read_bytes()
    if damage == "duplicate_key":
        raw = (
            raw.replace(b'"seq": 0', b'"seq": 0, "seq": 0', 1)
            if b'"seq": 0' in raw
            else raw.replace(b'"seq":0', b'"seq":0,"seq":0', 1)
        )
    elif damage == "bad_utf8":
        raw = raw.replace(b'"task"', b'"\xff"', 1)
    elif damage == "nonfinite":
        raw = raw.replace(b'"max_model_requests":8', b'"max_model_requests":NaN', 1)
    elif damage == "middle_line":
        parts = raw.splitlines(keepends=True)
        raw = parts[0] + b'{"broken":\n' + b"".join(parts[1:])
    else:
        raw += b'{"unexpected"'
    path.write_bytes(raw)
    with pytest.raises(RecordingError):
        read_recording(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "tail", [b"", b'{"seq":', b'{"seq":3,"type":"unfinished"}', b'{"text":"\xe4']
)
def test_missing_finish_or_torn_tail_remains_incomplete_and_read_only(completed_recording, tail):
    path, events = completed_recording
    rewrite(path, events[:-1])
    before = path.read_bytes() + tail
    path.write_bytes(before)
    result = read_recording(path)
    assert not result.complete and result.torn_tail is bool(tail)
    assert len(result.events) == len(events) - 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("limit", ["bytes", "event_bytes", "events"])
def test_reader_enforces_each_resource_limit(completed_recording, limit):
    path, events = completed_recording
    raw = path.read_bytes()
    limits = RecordingLimits()
    if limit == "bytes":
        limits = replace(limits, max_bytes=len(raw) - 1, max_event_bytes=len(raw) - 1)
    elif limit == "event_bytes":
        limits = replace(limits, max_event_bytes=100)
    else:
        limits = replace(limits, max_events=2)
    with pytest.raises(RecordingError):
        read_recording(path, limits)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("value", ["depth", "nodes", "surrogate", "object"])
def test_writer_rejects_unbounded_or_non_json_values_without_appending(completed_recording, value):
    path, _ = completed_recording
    seed = read_recording(path).seed
    other = path.with_name("invalid.jsonl")
    with RecordingWriter(other, seed) as writer:
        before = other.read_bytes()
        data = "x"
        if value == "depth":
            for _ in range(34):
                data = [data]
        elif value == "nodes":
            data = [0] * 200001
        elif value == "surrogate":
            data = "\ud800"
        else:
            data = object()
        with pytest.raises(RecordingError):
            writer.append("observation", {"channel": "context", "value": {"data": data}})
        assert other.read_bytes() == before
        with pytest.raises(RecordingError):
            writer.append("observation", {"channel": "context", "value": {}})


def test_tool_result_must_match_actual_execution_event(tmp_path):
    (tmp_path / "note.txt").write_text("real data", encoding="utf-8")

    def respond(body, number):
        value = (
            tool_response([call("read", "read_file", path="note.txt")]) if number == 1 else answer()
        )
        return httpx.Response(200, json=value)

    with recording_app(tmp_path, respond) as (app, recorder, _, path, _, _):
        recorder.run(lambda: app.send("task"))
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    tool = next(
        item
        for item in events
        if item["type"] == "call_finished" and isinstance(item["data"]["value"], list)
    )
    tool["data"]["value"][0]["content"] = '{"ok":true,"text":"invented"}'
    rewrite(path, events)
    with pytest.raises(RecordingError):
        read_recording(path)


def test_snapshot_is_frozen_when_written(completed_recording):
    path, _ = completed_recording
    seed = read_recording(path).seed
    other = path.with_name("snapshot.jsonl")
    with RecordingWriter(other, seed):
        seed["controls"]["agent"]["max_model_requests"] = 99
        seed["session_events"][0]["payload"]["title"] = "changed"
    saved = read_recording(other).seed
    assert saved["controls"]["agent"]["max_model_requests"] == 8
    assert saved["session_events"][0]["payload"]["title"] == "recorded"


def test_error_descriptor_does_not_copy_exception_message_or_attributes():
    error = LLMError("private-token")
    error.transport_headers = {"Authorization": "private-token"}
    encoded = json.dumps(error_record(error))
    assert "private-token" not in encoded and "transport_headers" not in encoded

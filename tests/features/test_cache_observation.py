"""B03.1：真实请求/文件/日志和重放，缓存观测与预算总量分离；远端 HTTP 使用模拟。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.contracts.errors import AgentError, LLMError
from adam.llm.client import LLMClient
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_streaming import DONE, event, sse
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

CACHE = {"prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6}


def response(value, stream=False):
    """保留测试原用量 10+2，增加实际响应的缓存字段；SSE 同时覆盖最后内容块 usage。"""
    value["usage"].update(CACHE)
    if not stream:
        return httpx.Response(200, json=value)
    choice = value["choices"][0]
    message = choice["message"]
    delta = {"role": "assistant", "content": message["content"]}
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {"index": index, **item} for index, item in enumerate(message["tool_calls"])
        ]
    return sse([event(delta), event(reason=choice["finish_reason"], usage=value["usage"]), DONE])


@pytest.mark.parametrize("stream", [False, True])
def test_tool_loop_records_cache_and_actual_input_prefix_then_replays_same_observations(
    tmp_path, monkeypatch, stream
):
    (tmp_path / "evidence.txt").write_text("source fact", encoding="utf-8")
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=stream))

    def respond(body, number):
        value = (
            tool_response([call("read", "read_file", path="evidence.txt")])
            if number == 1
            else answer("source fact")
        )
        return response(value, stream)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "source fact"
        sid = app.session.session_id
        events = store.load_events(sid)
        usages = [e["payload"] for e in events if e["type"] == "request_usage"]
        assert store.usage(sid) == (24, False), "cache hits must not subtract from the token budget"
        assert len(usages) == len(bodies) == 2
        assert [u["cache_usage"]["hit_tokens"] for u in usages] == [4, 4]
        first, second = [u["input_prefix"] for u in usages]
        assert first["common_prefix_bytes"] is None
        assert second["previous_sha256"] == first["input_sha256"]
        assert second["previous_sequence"] == first["sequence"] == 1
        assert second["common_prefix_bytes"] > 0
        path = next(config.sessions_dir.glob("*/*/*/*.jsonl"))
        requests = read_report(path)["turns"][-1]["requests"]
        assert [r["input_prefix"] for r in requests] == [first, second]
        assert "source fact" not in json.dumps(second)
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    replayed = read_report(next((tmp_path / "offline").glob("*/*/*/*.jsonl")))
    assert [r["input_prefix"] for r in replayed["turns"][-1]["requests"]] == [first, second]


@pytest.mark.parametrize("fault", ["disconnect", "interrupt", "protocol"])
def test_stream_failure_preserves_received_cache_usage_and_prefix_in_log_and_replay(
    tmp_path, monkeypatch, fault
):
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=True))

    def respond(body, number):
        usage = {"prompt_tokens": 10, "completion_tokens": 2, **CACHE}
        broken = {
            "disconnect": httpx.ReadError("private transport detail"),
            "interrupt": KeyboardInterrupt(),
            "protocol": b"data: invalid-json\n\n",
        }[fault]
        return sse([event({"content": "partial"}), event(reason="stop", usage=usage), broken])

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, _):
        with pytest.raises((LLMError, KeyboardInterrupt)):
            recorder.run(lambda: app.send("task"))
        assert [m.content for m in app.history()] == ["old question", "old answer"]
        assert store.usage(app.session.session_id) == (12, False)
        usage = [
            e["payload"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_usage"
        ][-1]
        assert usage["cache_usage"]["hit_tokens"] == 4
        assert usage["input_prefix"]["sequence"] == 1
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


def test_compaction_and_chat_share_client_observation_but_keep_distinct_logs(tmp_path):
    config = config_at(tmp_path)
    config = replace(config, context=replace(config.context, retain_recent_turns=0))
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: response(answer("summary or reply")))
        ) as http,
    ):
        sid = store.create_session("summary", "system")
        store.select_session(sid)
        store.append_turn(sid, "old question", "old answer")
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            app.resume(sid)
            assert app.compact() is not None
            assert app.send("new question") == "summary or reply"
            events = store.load_events(sid)
            compact = next(e["payload"] for e in events if e["type"] == "compaction_usage")
            chat = next(e["payload"] for e in events if e["type"] == "request_usage")
            assert compact["cache_usage"] == chat["cache_usage"]
            assert (
                chat["input_prefix"]["previous_sha256"] == compact["input_prefix"]["input_sha256"]
            )
            assert store.usage(sid) == (24, False)
            report = read_report(next(config.sessions_dir.glob("*/*/*/*.jsonl")))
            assert report["compactions"][0]["request"]["cache_usage"]["hit_tokens"] == 4
        finally:
            app.close()


def test_invalid_cache_does_not_make_known_usage_unknown_or_block_chat(tmp_path):
    def respond(body, number):
        value = answer("ok")
        value["usage"].update(prompt_cache_hit_tokens=9, prompt_cache_miss_tokens=9)
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, _, __):
        assert recorder.run(lambda: app.send("task")) == "ok"
        assert store.usage(app.session.session_id) == (12, False)
        usage = [
            e["payload"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_usage"
        ][-1]
        assert "cache_usage" not in usage and "input_prefix" in usage


@pytest.mark.parametrize("damage", ["bool_cache", "layout", "model"])
def test_corrupt_persisted_observation_is_rejected_without_repair(tmp_path, damage):
    with captured_app(tmp_path, lambda *_: response(answer("ok"))) as (app, recorder, store, _, __):
        recorder.run(lambda: app.send("task"))
        sid = app.session.session_id
        events = store.load_events(sid)
    usage = next(e["payload"] for e in events if e["type"] == "request_usage")
    if damage == "bool_cache":
        usage["cache_usage"]["hit_tokens"] = True
    else:
        usage["input_prefix"]["layout_version" if damage == "layout" else "model"] = "wrong"
    directory = tmp_path / "damaged"
    directory.mkdir()
    path = directory / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AgentError):
        read_report(path)
    assert path.read_bytes() == before


def test_legacy_recording_without_observation_fields_still_replays(tmp_path, monkeypatch):
    with captured_app(tmp_path, lambda *_: response(answer("ok"))) as (
        app,
        recorder,
        _,
        source,
        __,
    ):
        recorder.run(lambda: app.send("task"))

    def remove_new_fields(value):
        if isinstance(value, dict):
            return {
                k: remove_new_fields(v)
                for k, v in value.items()
                if k not in {"cache_usage", "input_prefix"}
            }
        if isinstance(value, list):
            return [remove_new_fields(v) for v in value]
        return value

    legacy = tmp_path / "legacy.jsonl"
    events = [remove_new_fields(json.loads(line)) for line in source.read_bytes().splitlines()]
    legacy.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    forbid_external_actions(monkeypatch)
    assert replay_turn(legacy, tmp_path / "offline")["status"] == "matched"


def test_parent_and_child_keep_independent_prefix_scope_with_shared_total_budget(
    tmp_path, monkeypatch
):
    def child(request):
        return response(answer("child evidence"))

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="read-only task")]
            )
        elif number == 2:
            spawned = json.loads(
                next(m["content"] for m in body["messages"] if m["role"] == "tool")
            )
            value = tool_response([call("wait", "wait_agents", agent_ids=[spawned["agent_id"]])])
        else:
            value = answer("collected child evidence")
        return response(value)

    with captured_app(tmp_path, respond, child_handler=child) as (app, recorder, _, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "collected child evidence"
        assert len(bodies) == 3 and app.usage().tokens == 48
        sid = app.session.session_id
    reports = [read_report(path) for path in config_at(tmp_path).sessions_dir.rglob("*.jsonl")]
    assert len(reports) == 2
    parent = next(r for r in reports if r["session_id"] == sid)["turns"][-1]["requests"]
    child_requests = next(r for r in reports if r["session_id"] != sid)["turns"][-1]["requests"]
    assert parent[0]["input_prefix"]["scope_id"] != child_requests[0]["input_prefix"]["scope_id"]
    assert child_requests[0]["input_prefix"]["common_prefix_bytes"] is None
    assert child_requests[0]["cache_usage"]["hit_tokens"] == 4
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


def test_observation_save_failure_does_not_commit_answer_or_guess_zero_cost(tmp_path, monkeypatch):
    from adam.contracts.errors import StorageError, TurnSaveError

    with captured_app(tmp_path, lambda *_: response(answer("unsaved reply"))) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):

        def fail_settlement(*args, **kwargs):
            # 故障注入在已有模型结果之后、实际结算块之前，模拟日志写入失败。
            assert kwargs["cache_usage"].hit_tokens == 4
            assert kwargs["input_prefix"].sequence == 1
            raise StorageError("synthetic settlement failure")

        monkeypatch.setattr(store, "finish_request", fail_settlement)
        with pytest.raises(TurnSaveError) as raised:
            recorder.run(lambda: app.send("task"))
        assert raised.value.partial_reply == "unsaved reply"
        assert len(bodies) == 1 and store.usage(app.session.session_id)[1] is True
        assert [m.content for m in app.history()] == ["old question", "old answer"]
        assert not any(
            e["type"] == "request_usage" for e in store.load_events(app.session.session_id)
        )


def test_invalid_nonstream_reply_retains_cache_statistics_and_replays_failure(
    tmp_path, monkeypatch
):
    def respond(body, number):
        value = answer("partial")
        value["choices"][0]["finish_reason"] = "length"
        return response(value)

    with captured_app(tmp_path, respond) as (app, recorder, store, source, _):
        with pytest.raises(LLMError) as raised:
            recorder.run(lambda: app.send("task"))
        assert raised.value.cache_usage.hit_tokens == 4
        assert raised.value.input_prefix.sequence == 1
        assert store.usage(app.session.session_id) == (12, False)
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"

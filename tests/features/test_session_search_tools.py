"""B08.2：假 HTTP 驱动真实历史检索，验证来源、范围、压缩和工具执行边界。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import AgentConfig, SessionSearchConfig, ToolDiscoveryConfig, ToolLimits
from adam.contracts.errors import BudgetError, ConfigError
from adam.recording.runtime import replay_turn
from adam.session.outcome import ExecutionOutcome
from adam.session.search import SessionSearchIndex
from tests.features.test_memory_context import config_at
from tests.features.test_session_search import seed
from tests.features.test_subagents import application as delegated_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def read_call(hit, **options):
    reference = {key: hit[key] for key in ("session_id", "turn_id", "source_sha256", "kind")}
    if hit["message_index"] is not None:
        reference["message_index"] = hit["message_index"]
    return call("read-source", "read_session", **reference, **options)


def test_model_recovers_compacted_original_through_discovery_and_logs_sources(tmp_path):
    config = replace(config_at(tmp_path), tool_discovery=ToolDiscoveryConfig())
    original = "ORION_ORIGINAL 不得自动发布；必须人工确认。"
    found = []

    def respond(body, number):
        if "tools" not in body:
            return answer("摘要只保留项目代号；具体约束请查原文。")
        if number == 2:
            assert original not in json.dumps(body["messages"], ensure_ascii=False)
            return tool_response(
                [call("discover", "discover_tools", names=["search_sessions", "read_session"])]
            )
        if number == 3:
            return tool_response([call("search", "search_sessions", query="ORION_ORIGINAL")])
        if number == 4:
            result = outputs(body)[-1]
            assert result["complete"] and result["hits"][0]["text"] == original
            found.append(result["hits"][0])
            return tool_response([read_call(found[0])])
        assert outputs(body)[-1]["excerpt"]["text"] == original
        return answer("原文要求人工确认。")

    with application(tmp_path, respond, config=config) as (app, store, requests):
        sid = app.new().session_id
        for number in range(5):
            store.append_turn(sid, f"old {number}", original if number == 0 else "unrelated")
        app.resume(sid)
        summary = app.compact()
        assert len(summary.covered_turn_ids) == 3
        assert app.send("查压缩前的原始约束") == "原文要求人工确认。"
        assert found[0]["turn_id"] == summary.covered_turn_ids[0]
        events = store.load_events(sid)
        tool_events = [e["payload"] for e in events if e["type"] == "tool_started"]
        assert [e["name"] for e in tool_events] == [
            "discover_tools",
            "search_sessions",
            "read_session",
        ]
        assert len({e["turn_id"] for e in tool_events}) == 1
        assert len({e["request_id"] for e in tool_events}) == 3
        last = store.load_turns(sid)[-1]
        assert (
            len(requests) == 5 and store.execution_report(sid, last.turn_id).status == "succeeded"
        )


def test_allowed_other_session_and_diagnostics_are_returned_as_tool_data_only(tmp_path):
    config = config_at(tmp_path)
    sid, _, _ = seed(config.sessions_dir, "ORION_SHARED approved historical text")
    denied, turn, _ = seed(config.sessions_dir, "ORION_SHARED PRIVATE_OTHER")
    config = replace(config, session_search=SessionSearchConfig(allowed_session_ids=(sid,)))

    def respond(body, number):
        if number == 1:
            return tool_response([call("search", "search_sessions", query="ORION_SHARED")])
        if number == 2:
            hits = outputs(body)[-1]["hits"]
            assert {h["session_id"] for h in hits} == {sid}
            assert "PRIVATE_OTHER" not in json.dumps(body)
            return tool_response(
                [
                    call(
                        "forbidden",
                        "read_session",
                        session_id=denied,
                        turn_id=turn,
                        message_index=2,
                        source_sha256=hits[0]["source_sha256"],
                    )
                ]
            )
        if number == 3:
            assert outputs(body)[-1]["ok"] is False
            return tool_response(
                [
                    call(
                        "diagnostic",
                        "search_sessions",
                        query="FAILED_CORRECTION",
                        include_diagnostics=True,
                    )
                ]
            )
        result = outputs(body)[-1]
        assert len(result["hits"]) == 1
        hit = result["hits"][0]
        assert hit["kind"] == "diagnostic_input" and hit["status"] == "failed"
        assert all(
            "FAILED_CORRECTION" not in (m.get("content") or "")
            for m in body["messages"]
            if m["role"] in {"user", "assistant"}
        )
        return answer("已区分诊断输入")

    with application(tmp_path, respond, config=config) as (app, store, _):
        current = app.new().session_id
        failed = store.start_turn(current, "FAILED_CORRECTION 尚未成功处理")
        store.finish_turn(current, failed, ExecutionOutcome.failure(ValueError()))
        assert app.send("查授权历史以及失败诊断") == "已区分诊断输入"
        assert len(store.load_turns(current)) == 1


def test_switching_current_session_revokes_implicit_access_to_previous_one(tmp_path):
    expected = [None]

    def respond(body, number):
        if number % 2:
            return tool_response([call("search", "search_sessions", query="LOCAL_ONLY")])
        result = outputs(body)[-1]
        assert {h["session_id"] for h in result["hits"]} == (
            {expected[0]} if expected[0] else set()
        )
        return answer("checked")

    with application(tmp_path, respond, config=config_at(tmp_path)) as (app, store, _):
        first = app.new().session_id
        store.append_turn(first, "earlier", "LOCAL_ONLY private first session")
        app.resume(first)
        expected[0] = first
        app.send("search earlier")
        app.new()
        expected[0] = None
        app.send("search earlier")


@pytest.mark.parametrize("maximum", [512, 1400])
def test_actual_tool_output_budget_preserves_valid_source_or_reports_failure(tmp_path, maximum):
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=maximum))
    text = "ORION_BOUNDED " + '"\\\n汉字' * 600

    def respond(body, number):
        if number == 1:
            return tool_response([call("search", "search_sessions", query="ORION_BOUNDED")])
        assert all(len(m["content"]) <= maximum for m in body["messages"] if m["role"] == "tool")
        result = outputs(body)[-1]
        if maximum == 512:
            assert not result["ok"] and "限额不足" in result["error"]
            return answer("cannot fit")
        if number == 2:
            hit = result["hits"][0]
            assert hit["text"] == text[hit["start"] : hit["end"]]
            return tool_response([read_call(hit, start=hit["end"])])
        excerpt = result["excerpt"]
        assert excerpt["text"] == text[excerpt["start"] : excerpt["end"]]
        assert result["next_offset"] == excerpt["end"]
        return answer("continued")

    with application(tmp_path, respond, config=config) as (app, store, _):
        sid = app.new().session_id
        store.append_turn(sid, "old", text)
        app.resume(sid)
        app.send("find bounded evidence")


def test_disabled_tools_are_absent_and_cannot_be_guessed(tmp_path):
    config = replace(config_at(tmp_path), session_search=SessionSearchConfig(enabled=False))

    def respond(body, number):
        assert not {t["function"]["name"] for t in body["tools"]} & {
            "search_sessions",
            "read_session",
        }
        if number == 1:
            return tool_response([call("guess", "search_sessions", query="anything")])
        assert outputs(body)[-1]["ok"] is False
        return answer("disabled")

    with application(tmp_path, respond, config=config) as (app, _, _):
        app.send("inspect")


def test_history_query_uses_existing_tool_budget_and_plan_mode(tmp_path):
    config = replace(config_at(tmp_path), agent=AgentConfig(max_tool_calls=1))

    def respond(body, number):
        return tool_response([call(f"search-{number}", "search_sessions", query="history")])

    with application(tmp_path, respond, config=config) as (app, store, requests):
        app.select_mode("plan")
        with pytest.raises(BudgetError):
            app.send("查历史")
        events = store.load_events(app.session.session_id)
        assert len([e for e in events if e["type"] == "tool_started"]) == 1
        assert len(requests) == 2


def test_replay_uses_frozen_tool_result_and_never_requeries_current_history(tmp_path, monkeypatch):
    def respond(body, number):
        result = (
            tool_response([call("search", "search_sessions", query="old")])
            if number == 1
            else answer("from history")
        )
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond) as (app, recorder, _, path, _):
        assert recorder.run(lambda: app.send("task")) == "from history"
    forbid_external_actions(monkeypatch)

    def forbid(*args, **kwargs):
        pytest.fail("replay must not query today's sessions")

    monkeypatch.setattr(SessionSearchIndex, "search", forbid)
    result = replay_turn(path, tmp_path / "offline")
    assert result["status"] == "matched", result


@pytest.mark.parametrize(
    "value", ['"1234abcd"', '["../escape"]', '["1234abcd", "1234abcd"]', "[true]"]
)
def test_bad_authorization_config_is_rejected_before_application(tmp_path, value):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(f"[session_search]\nallowed_session_ids={value}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_toml_authorization_is_explicit_and_validated(tmp_path):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        '[session_search]\nenabled=false\nallowed_session_ids=["1234abcd"]', encoding="utf-8"
    )
    assert load_config(path).session_search == SessionSearchConfig(False, ("1234abcd",))


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_history_tool_keeps_real_hook_before_and_after_boundaries(tmp_path, monkeypatch, decision):
    config = replace(config_at(tmp_path), hooks=hooks_at(tmp_path, before=decision))
    if decision == "deny":

        def forbid(*args, **kwargs):
            pytest.fail("denied query reached the session index")

        monkeypatch.setattr(SessionSearchIndex, "search", forbid)

    def respond(body, number):
        if number == 1:
            return tool_response([call("search", "search_sessions", query="history")])
        result = outputs(body)[-1]
        assert result["ok"] is (decision == "allow")
        if decision == "deny":
            assert result["status"] == "hook_blocked"
        return answer("checked hooks")

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.send("inspect history")
        hooks = store.load_hooks(app.session.session_id)
        assert len(hooks) == (2 if decision == "allow" else 1)
        received = json.loads((tmp_path / "before_tool-search.json").read_text(encoding="utf-8"))
        assert received["tool"]["name"] == "search_sessions"


def test_child_does_not_inherit_parent_history_tool_or_extra_authorization(tmp_path):
    config = config_at(tmp_path)
    sid, _, _ = seed(config.sessions_dir, "PARENT_PRIVATE shared only with parent")
    config = replace(config, session_search=SessionSearchConfig(allowed_session_ids=(sid,)))
    parent_bodies, child_bodies = [], []

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        if len(parent_bodies) == 1:
            result = tool_response(
                [
                    call(
                        "spawn",
                        "spawn_agent",
                        name="helper",
                        task="inspect available history tools",
                    )
                ]
            )
        elif len(parent_bodies) == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            result = answer("done")
        return httpx.Response(200, json=result)

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        names = {item["function"]["name"] for item in body["tools"]}
        assert not names & {"search_sessions", "read_session"}
        assert "shared only with parent" not in json.dumps(body)
        if len(child_bodies) == 1:
            result = tool_response([call("guess", "search_sessions", query="PARENT_PRIVATE")])
        else:
            assert not outputs(body)[-1]["ok"]
            result = answer("history not authorized")
        return httpx.Response(200, json=result)

    with delegated_application(tmp_path, parent, child, config=config) as (app, _):
        assert app.send("delegate a bounded check") == "done"
    assert len(child_bodies) == 2

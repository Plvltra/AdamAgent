"""B01.2：模拟 HTTP 驱动生产工具、真实文件/进程、子任务与 JSONL；不调用付费模型。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import AgentConfig, NoProgressConfig, ReflectionConfig, ToolLimits
from adam.contracts.errors import (
    BudgetError,
    LLMError,
    NoProgressError,
    RequestInterrupted,
    StorageError,
)
from adam.recording.journal import read_recording
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_subagents import application as delegated_app
from tests.features.test_subagents import call, index_for, outputs
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_discovery_loop import application
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def tool_outputs(events):
    return [json.loads(e["payload"]["output"]) for e in events if e["type"] == "tool_finished"]


def repeated_read(body, number):
    """默认按需模式先发现，之后始终用新 ID 重试同一缺失文件。"""
    if number == 1:
        return tool_response([call("discover", "discover_tools", names=["read_file"])])
    assert number <= 4, "guard must stop before another model request"
    if number == 4:
        assert outputs(body)[-1]["no_progress"]["action"] == "warn"
        assert "换策略" in outputs(body)[-1]["no_progress"]["hint"]
    return tool_response([call(f"missing-{number}", "read_file", path="missing.txt")])


def test_default_chat_warns_model_then_persists_stop_without_committing_failed_input(tmp_path):
    with application(tmp_path, repeated_read) as (app, store, bodies):
        with pytest.raises(NoProgressError, match="连续失败 3 次"):
            app.send("find the missing file")
        sid = app.session.session_id
        events = store.load_events(sid)
        observed = [r["no_progress"] for r in tool_outputs(events) if "no_progress" in r]
        assert [r["action"] for r in observed] == ["continue", "warn", "stop"]
        assert [r["repeats"] for r in observed] == [1, 2, 3]
        assert len({r["fingerprint"] for r in observed}) == 1
        assert len(bodies) == 4 and store.usage(sid) == (48, False)
        assert not store.load_messages(sid)
        assert events[-1]["payload"]["reason"] == "no_progress"
        assert events[-1]["payload"]["status"] == "failed"
        assert events[-1]["payload"]["duration_ms"] >= 0
        assert all(
            e["payload"]["status"] == "succeeded" for e in events if e["type"] == "request_finished"
        )


@pytest.mark.parametrize("repair", ["parameters", "external_file"])
def test_warning_allows_changed_strategy_or_real_external_repair(tmp_path, repair):
    def respond(body, number):
        if number < 4:
            return repeated_read(body, number)
        if number == 4:
            assert outputs(body)[-1]["no_progress"]["action"] == "warn"
            name = "alternative.txt" if repair == "parameters" else "missing.txt"
            (tmp_path / name).write_text("actual evidence", encoding="utf-8")
            return tool_response([call("repaired", "read_file", path=name)])
        assert "no_progress" not in outputs(body)[-1]
        assert outputs(body)[-1]["lines"][0]["text"] == "actual evidence"
        return answer("verified evidence")

    with application(tmp_path, respond) as (app, store, bodies):
        assert app.send("investigate") == "verified evidence"
        events = store.load_events(app.session.session_id)
        assert events[-1]["payload"]["reason"] == "completed"
        assert len(bodies) == 5
        assert any(
            '"action": "warn"' in str(m) for m in store.load_messages(app.session.session_id)
        )


def test_disabled_guard_keeps_original_request_limit_and_no_extra_requests(tmp_path):
    config = replace(
        config_at(tmp_path),
        no_progress=NoProgressConfig(enabled=False),
        agent=AgentConfig(max_model_requests=4),
    )
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert all("no_progress" not in result for result in outputs(body))
        return httpx.Response(
            200, json=tool_response([call(str(len(bodies)), "read_file", path="missing")])
        )

    with plan_app(tmp_path, respond, config=config) as (app, store):
        with pytest.raises(BudgetError) as error:
            app.send("retry")
        assert error.value.code == "request_limit" and len(bodies) == 4
        assert store.load_events(app.session.session_id)[-1]["payload"]["reason"] == "request_limit"


def test_new_user_turn_starts_at_one_after_previous_turn_stopped(tmp_path):
    def respond(body, number):
        if number <= 4:
            return repeated_read(body, number)
        if number == 5:
            return tool_response([call("discover-again", "discover_tools", names=["read_file"])])
        if number == 6:
            return tool_response([call("first-again", "read_file", path="missing.txt")])
        assert outputs(body)[-1]["no_progress"]["repeats"] == 1
        assert "first task" not in json.dumps(body)
        return answer("still missing")

    with application(tmp_path, respond) as (app, store, bodies):
        with pytest.raises(NoProgressError):
            app.send("first task")
        assert app.send("new task") == "still missing"
        assert len(bodies) == 7
        turns = [
            e["payload"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "turn_started"
        ]
        assert len({t["turn_id"] for t in turns}) == 2


def test_saving_warning_failure_stays_storage_error_and_never_retries(tmp_path, monkeypatch):
    with application(tmp_path, repeated_read) as (app, store, bodies):
        original = store.append_tool_event

        def fail_warning(session_id, kind, payload):
            # 注入 tool_finished 的真实保存边界；此前工具读取和开始事件照常发生。
            if (
                kind == "tool_finished"
                and json.loads(payload["output"]).get("no_progress", {}).get("action") == "warn"
            ):
                raise StorageError("injected warning write failure")
            original(session_id, kind, payload)

        monkeypatch.setattr(store, "append_tool_event", fail_warning)
        with pytest.raises(StorageError, match="injected"):
            app.send("inspect")
        events = store.load_events(app.session.session_id)
        assert events[-1]["payload"]["reason"] == "storage_error" and len(bodies) == 3
        assert len([e for e in events if e["type"] == "tool_started"]) == 3
        assert len(tool_outputs(events)) == 2


@pytest.mark.parametrize(
    "failure,expected", [("cancel", "interrupted"), ("timeout", "api_timeout")]
)
def test_cancel_or_unknown_usage_after_warning_is_not_overwritten_by_guard(
    tmp_path, failure, expected
):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 3:
            assert outputs(bodies[-1])[-1]["no_progress"]["action"] == "warn"
            if failure == "cancel":
                raise RequestInterrupted((0, 0))
            raise httpx.ReadTimeout("injected wait failure", request=request)
        return httpx.Response(
            200, json=tool_response([call(str(len(bodies)), "read_file", path="missing")])
        )

    with plan_app(tmp_path, respond) as (app, store):
        with pytest.raises(RequestInterrupted if failure == "cancel" else LLMError):
            app.send("inspect")
        assert store.load_events(app.session.session_id)[-1]["payload"]["reason"] == expected
        assert len(bodies) == 3 and not store.load_messages(app.session.session_id)
        if failure == "timeout":
            assert store.usage(app.session.session_id)[1]
            with pytest.raises(BudgetError):
                app.send("must not retry unknown consumption")
            assert len(bodies) == 3


def test_reflection_does_not_reset_repeated_tool_failures(tmp_path):
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" not in body:
            return httpx.Response(200, json=answer(json.dumps(SUGGESTION)))
        return httpx.Response(
            200, json=tool_response([call(str(len(bodies)), "read_file", path="missing")])
        )

    config = replace(config_at(tmp_path), reflection=ReflectionConfig())
    with plan_app(tmp_path, respond, config=config) as (app, store):
        with pytest.raises(NoProgressError):
            app.send("inspect")
        assert len(bodies) == 4 and sum("tools" not in b for b in bodies) == 1
        observed = [
            r["no_progress"]["repeats"]
            for r in tool_outputs(store.load_events(app.session.session_id))
        ]
        assert observed == [1, 2, 3]


def test_nonzero_command_long_output_retains_feedback_and_original_artifact(tmp_path):
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=512))
    bodies, approvals = [], []
    argv = [sys.executable, "-c", "import sys; print('evidence' * 900); sys.exit(9)"]

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200, json=tool_response([call(str(len(bodies)), "run_command", argv=argv)])
        )

    with plan_app(
        tmp_path, respond, config=config, confirm=lambda r: approvals.append(r) or True
    ) as (app, store):
        with pytest.raises(NoProgressError):
            app.send("run command")
        results = tool_outputs(store.load_events(app.session.session_id))
        assert len(approvals) == len(bodies) == 3
        # 验收实际保存并发送的编码；重新 dumps 会添加不存在于请求中的空格。
        assert all(
            len(event["payload"]["output"]) <= 512
            for event in store.load_events(app.session.session_id)
            if event["type"] == "tool_finished"
        )
        for index, result in enumerate(results, start=1):
            assert result["ok"] and result["exit_code"] == 9 and result["status"] == "completed"
            assert result["no_progress"]["repeats"] == index
            reference = store.load_artifact(
                app.session.session_id, result["artifact"]["artifact_id"]
            )
            assert reference is not None and reference.complete
        assert results[1]["no_progress"]["action"] == "warn"
        assert results[2]["no_progress"]["action"] == "stop"


def test_child_inherits_threshold_and_parent_collects_failure_normally(tmp_path):
    parent_bodies, child_bodies = [], []

    def child(request):
        child_bodies.append(json.loads(request.content))
        assert len(child_bodies) <= 4
        return httpx.Response(
            200, json=tool_response([call(str(len(child_bodies)), "read_file", path="missing")])
        )

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        if len(parent_bodies) == 1:
            data = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="inspect missing")]
            )
        elif len(parent_bodies) == 2:
            data = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "failed"
            data = answer("child could not find the file")
        return httpx.Response(200, json=data)

    config = replace(config_at(tmp_path), no_progress=NoProgressConfig(stop_after=4))
    with delegated_app(tmp_path, parent, child, config=config) as (app, store):
        assert app.send("delegate") == "child could not find the file"
        index = json.loads(index_for(app).read_text(encoding="utf-8"))
        child_id = index["agents"][0]["agent_id"]
        root = index_for(app).parent / "sessions"
        with SessionStore(root) as children:
            events = children.load_events(child_id)
        assert events[-1]["payload"]["reason"] == "no_progress"
        assert [r["no_progress"]["repeats"] for r in tool_outputs(events)] == [1, 2, 3, 4]
        assert store.load_events(app.session.session_id)[-1]["payload"]["reason"] == "completed"
    assert len(child_bodies) == 4 and len(parent_bodies) == 3


def test_offline_replay_preserves_guard_stop_without_external_actions(tmp_path, monkeypatch):
    def respond(body, number):
        return httpx.Response(
            200, json=tool_response([call(str(number), "read_file", path="missing")])
        )

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        with pytest.raises(NoProgressError):
            recorder.run(lambda: app.send("task"))
        assert len(bodies) == 3
        sid = app.session.session_id
        expected_usage = store.usage(sid)
        expected = tool_outputs(store.load_events(sid))
    assert read_recording(source).complete
    original = source.read_bytes()
    forbid_external_actions(monkeypatch)
    report = replay_turn(source, tmp_path / "offline")
    assert report["status"] == "matched" and report["error"]["code"] == "no_progress", report
    with SessionStore(tmp_path / "offline") as store:
        assert store.usage(sid) == expected_usage
        assert tool_outputs(store.load_events(sid)) == expected
    assert source.read_bytes() == original

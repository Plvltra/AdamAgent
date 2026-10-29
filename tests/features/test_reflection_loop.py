"""E4.3：假 HTTP 驱动真实工具和日志，反思不替代执行证据。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import ReflectionConfig
from adam.contracts.errors import ApprovalDeniedError, LLMError
from tests.features.test_memory_context import config_at
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_loop import answer, tool_response


def reflection_config(root):
    return replace(config_at(root), reflection=ReflectionConfig())


def advice_messages(body):
    return [
        m
        for m in body["messages"]
        if isinstance(m.get("content"), str) and m["content"].startswith("以下是程序记录的本轮反思")
    ]


@pytest.mark.parametrize("mode", ["valid", "bad_json", "bad_protocol", "unknown"])
def test_failed_tools_reflect_once_then_continue_without_polluting_history(tmp_path, mode):
    bodies = []
    (tmp_path / "exists.txt").write_text("real evidence", encoding="utf-8")

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            data = tool_response(
                [
                    call("missing", "read_file", path="missing.txt"),
                    call("read", "read_file", path="exists.txt"),
                ]
            )
        elif len(bodies) == 2:
            assert "tools" not in body
            data = answer(json.dumps(SUGGESTION) if mode != "bad_json" else "bad json")
            if mode == "bad_protocol":
                data["choices"] = []
            if mode == "unknown":
                data.pop("usage")
        elif len(bodies) == 3:
            assert len(outputs(body)) == 2
            assert bool(advice_messages(body)) == (mode == "valid")
            data = tool_response([call("missing2", "read_file", path="still-missing.txt")])
        else:
            data = answer("结束；失败文件仍未找到")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, config=reflection_config(tmp_path)) as (app, store):
        if mode == "unknown":
            with pytest.raises(LLMError):
                app.send("检查文件")
            assert len(bodies) == 2
            assert not store.load_turns(app.session.session_id)
            assert app.usage().unknown
            return
        assert app.send("检查文件") == "结束；失败文件仍未找到"
        sid = app.session.session_id
        records = store.load_reflections(sid)
        assert len(records) == 1
        assert records[0]["status"] == ("completed" if mode == "valid" else "invalid")
        assert len(bodies) == 4 and app.usage().tokens == 48
        assert not any("以下是程序记录的本轮反思" in str(m) for m in app.history())
        assert not app.preview_context("新问题").reflections
        assert app.preview_context().estimate.reflection_tokens == 0
        events = store.load_events(sid)
        starts = [i for i, e in enumerate(events) if e["type"] == "request_started"]
        ends = [i for i, e in enumerate(events) if e["type"] == "tool_finished"]
        assert ends[1] < starts[1]
        following = events[starts[2]]["payload"]["context"]
        assert bool(following.get("reflections")) == (mode == "valid")


def test_correction_after_restart_quotes_current_user_and_links_previous_turn(tmp_path):
    cfg = reflection_config(tmp_path)
    with plan_app(tmp_path, lambda _: httpx.Response(200, json=answer("旧回答")), config=cfg) as (
        app,
        store,
    ):
        app.send("配置路径基于哪里")
        sid = app.session.session_id
        previous_id = store.load_turns(sid)[0].turn_id
    bodies, notices = [], []
    quote = "你上次的路径基准错了"

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            data = tool_response([call("feedback", "report_feedback", quote=quote)])
        elif len(bodies) == 2:
            assert "tools" not in body and quote in json.dumps(body, ensure_ascii=False)
            data = answer(json.dumps(SUGGESTION))
        else:
            assert advice_messages(body)
            data = answer("我会先验证路径")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, config=cfg, notices=notices) as (app, store):
        app.resume(sid)
        app.send(quote + "，请重新检查")
        current = store.load_turns(sid)[-1].turn_id
        feedback = store.load_feedback(sid, current)[0]
        assert feedback.source_turn_id == previous_id and feedback.quote == quote
        assert len(bodies) == 3
        assert any("识别为对上一轮的纠正" in text for text in notices)


@pytest.mark.parametrize("previous,quote", [(False, "纠正"), (True, "伪造原文")])
def test_invalid_correction_does_not_trigger_reflection(tmp_path, previous, quote):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            data = tool_response([call("feedback", "report_feedback", quote=quote)])
        else:
            assert outputs(body)[-1]["ok"] is False
            data = answer("请说明")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, config=reflection_config(tmp_path)) as (app, store):
        if previous:
            sid = store.create_session("previous", "")
            store.append_turn(sid, "old", "answer")
            app.resume(sid)
        app.send("纠正")
        assert len(bodies) == 2 and store.load_reflections(app.session.session_id) == []


@pytest.mark.parametrize("deny", [False, True])
def test_no_reflection_when_denied_or_only_final_request_remains(tmp_path, deny):
    bodies = []
    cfg = reflection_config(tmp_path)
    cfg = replace(cfg, agent=replace(cfg.agent, max_model_requests=2))

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            tool = (
                call("run", "run_command", argv=[sys.executable, "-c", "print('x')"])
                if deny
                else call("read", "read_file", path="missing")
            )
            return httpx.Response(200, json=tool_response([tool]))
        return httpx.Response(200, json=answer())

    with plan_app(tmp_path, handler, config=cfg, confirm=lambda _: False) as (app, store):
        if deny:
            with pytest.raises(ApprovalDeniedError):
                app.send("运行")
            assert len(bodies) == 1 and not store.load_reflections(app.session.session_id)
        else:
            app.send("读取")
            assert len(bodies) == 2
            assert store.load_reflections(app.session.session_id)[0]["status"] == "skipped"

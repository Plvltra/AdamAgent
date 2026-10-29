"""E3.3：进度、只读查询、有限重规划和提前回答的流式收尾。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.cli.main import handle_command
from adam.cli.terminal import show_plan
from adam.config.settings import AgentConfig, PlanningConfig
from tests.features.test_memory_context import config_at
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import plan_app, step
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("max_requests", [2, 8])
def test_unfinished_final_is_reminded_at_most_once_and_never_exceeds_budget(tmp_path, max_requests):
    config = replace(config_at(tmp_path), agent=AgentConfig(max_model_requests=max_requests))
    requests, notices, plans = [], [], []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            data = tool_response([call("p1", "update_plan", steps=[step("in_progress")])])
        else:
            data = answer(f"reply {len(requests)}")
        if len(requests) == 3:
            assert "计划收尾提醒" in body["messages"][0]["content"]
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, config=config, notices=notices, plans=plans) as (app, store):
        shown = []
        handle_command("/plan", app, shown.append)
        assert "尚无计划" in shown[-1] and app.session is None
        reply = app.send("do work")
        assert reply == ("reply 2" if max_requests == 2 else "reply 3")
        assert sum("提醒模型" in notice for notice in notices) == (max_requests != 2)
        assert sum("计划未完成" in notice for notice in notices) == 1
        assert len(plans) == 1
        report = app.plan_report()
        assert report.snapshot.unfinished and report.turn_status == "succeeded"
        show_plan(report, shown.append)
        assert any("计划未完成" in line for line in shown)
        session_id = app.session.session_id
        assert store.load_messages(session_id)[-1].content == reply
        assert len(requests) == min(max_requests, 3)
        assert "计划收尾提醒" not in app.preview_context().messages[0]["content"]
        app.new("another")
        assert app.plan_report() is None
        assert "计划收尾提醒" not in app.preview_context().messages[0]["content"]
        handle_command("/plan unexpected", app, shown.append)
        assert shown[-1] == "用法：/plan"
        assert len(requests) == min(max_requests, 3)
    with plan_app(tmp_path, handler, config=config) as (app, store):
        app.resume(session_id)
        assert app.plan_report().snapshot.unfinished
        assert len(requests) == min(max_requests, 3)


def test_failed_tool_can_replan_but_excess_replans_keep_last_version(tmp_path):
    config = replace(config_at(tmp_path), planning=PlanningConfig(max_steps=2, max_replans=1))
    requests, plans = [], []
    revised = {**step("in_progress"), "content": "handle missing file"}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            data = tool_response(
                [
                    call("p1", "update_plan", steps=[step("in_progress")]),
                    call("read", "read_file", path="missing.txt"),
                ]
            )
        elif len(requests) == 2:
            data = tool_response(
                [call("p2", "update_plan", steps=[revised], reason="file missing")]
            )
        elif len(requests) == 3:
            data = tool_response(
                [
                    call(
                        "p3",
                        "update_plan",
                        steps=[{**revised, "content": "another approach"}],
                        reason="try again",
                    )
                ]
            )
        elif len(requests) == 4:
            assert outputs(body)[-1]["ok"] is False
            data = tool_response(
                [
                    call(
                        "p4",
                        "update_plan",
                        steps=[{**revised, "status": "cancelled", "reason": "no file"}],
                    )
                ]
            )
        else:
            data = answer("cannot continue")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, config=config, plans=plans) as (app, store):
        app.send("edit file")
        assert [p.snapshot.version for p in plans] == [1, 2, 3]
        final = app.plan_report().snapshot
        assert final.replan_count == 1 and final.steps[0].status == "cancelled"
        updates = [
            e["payload"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "plan_updated"
        ]
        assert [p["tool_call_id"] for p in updates] == ["p1", "p2", "p4"]


def test_streamed_premature_final_is_hidden_until_plan_is_closed(tmp_path):
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=True))
    bodies, delivered = [], []

    def plan_stream(call_id, entry):
        return sse(
            [
                event(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "update_plan",
                                    "arguments": json.dumps({"steps": [entry]}),
                                },
                            }
                        ]
                    }
                ),
                event(reason="tool_calls", usage=USAGE),
                DONE,
            ]
        )

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return plan_stream("start", step("in_progress"))
        if len(bodies) == 3:
            assert "计划收尾提醒" in body["messages"][0]["content"]
            return plan_stream("close", step("cancelled", reason="cannot finish"))
        text = "premature" if len(bodies) == 2 else "honest final"
        return sse([event({"content": text}), event(reason="stop", usage=USAGE), DONE])

    with plan_app(tmp_path, handler, config=config) as (app, store):
        assert app.send("work", on_output=delivered.append) == "honest final"
        assert [item.text for item in delivered if item.kind == "text"] == ["honest final"]
        assert all(
            "premature" not in message.content
            for message in store.load_messages(app.session.session_id)
        )
        assert app.plan_report().snapshot.steps[0].status == "cancelled"


def test_plan_display_failure_propagates_after_tool_result_is_saved(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=tool_response(
                [
                    call("plan", "update_plan", steps=[step()]),
                    call("later", "read_file", path="note.txt"),
                ]
            ),
        )

    class BrokenDisplay(list):
        def append(self, value):
            raise OSError("terminal disconnected")

    with plan_app(tmp_path, handler, plans=BrokenDisplay()) as (app, store):
        with pytest.raises(OSError, match="terminal disconnected"):
            app.send("work")
        assert store.load_plan(app.session.session_id).snapshot.version == 1
        events = store.load_events(app.session.session_id)
        assert [e["payload"]["tool_call_id"] for e in events if e["type"] == "tool_finished"] == [
            "plan"
        ]
        assert len(requests) == 1

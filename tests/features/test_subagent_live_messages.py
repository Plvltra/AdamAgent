"""B28.1：父模型在子请求运行中补充，按run/message身份去重并在安全点消费。"""

import json
import threading

import httpx

from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_parent_messages_active_child_and_old_run_cannot_receive_new_messages(tmp_path):
    ready, release, next_ready, next_release = (threading.Event() for _ in range(4))
    parent_steps, child_steps = [], []
    target = {}

    def child(request):
        body = json.loads(request.content)
        child_steps.append(body)
        assert "message_agent" not in [tool["function"]["name"] for tool in body.get("tools", [])]
        if len(child_steps) == 1:
            ready.set()
            assert release.wait(5)
            return httpx.Response(200, json=answer("old final"))
        if len(child_steps) == 2:
            message = body["messages"][-1]
            assert message["role"] == "user" and "extra requirement" in message["content"]
            assert (
                "message=note-1" in message["content"] and "不是用户新增授权" in message["content"]
            )
            assert (
                sum("extra requirement" in (item.get("content") or "") for item in body["messages"])
                == 1
            )
            return httpx.Response(200, json=answer("updated child result"))
        next_ready.set()
        assert next_release.wait(5)
        return httpx.Response(200, json=answer("second run result"))

    def parent(request):
        body = json.loads(request.content)
        parent_steps.append(body)
        step = len(parent_steps)
        results = outputs(body)
        if step == 1:
            value = tool_response(
                [call("spawn", "spawn_agent", name="worker", task="initial task")]
            )
        elif step == 2:
            assert ready.wait(3)
            target.update(results[-1])
            args = dict(
                agent_id=target["agent_id"],
                run_id=target["run_id"],
                message_id="note-1",
                text="extra requirement",
            )
            value = tool_response(
                [call("note", "message_agent", **args), call("retry", "message_agent", **args)]
            )
        elif step == 3:
            assert results[-2]["status"] == results[-1]["status"] == "accepted"
            assert not results[-2]["duplicate"] and results[-1]["duplicate"]
            assert results[-2]["sequence"] == results[-1]["sequence"] == 1
            release.set()
            value = tool_response(
                [call("wait", "wait_agents", agent_ids=[target["agent_id"]], timeout_seconds=5)]
            )
        elif step == 4:
            assert "updated child result" in json.dumps(results[-1])
            value = tool_response(
                [call("again", "send_message", agent_id=target["agent_id"], task="second task")]
            )
        elif step == 5:
            assert next_ready.wait(3)
            assert results[-1]["run_id"] != target["run_id"]
            value = tool_response(
                [
                    call(
                        "stale",
                        "message_agent",
                        agent_id=target["agent_id"],
                        run_id=target["run_id"],
                        message_id="late",
                        text="must not enter next run",
                    )
                ]
            )
        elif step == 6:
            assert results[-1]["ok"] is False and "run_id" in results[-1]["error"]
            next_release.set()
            value = tool_response(
                [call("wait2", "wait_agents", agent_ids=[target["agent_id"]], timeout_seconds=5)]
            )
        else:
            assert "second run result" in json.dumps(results[-1])
            value = answer("parent finished")
        return httpx.Response(200, json=value)

    try:
        with application(tmp_path, parent, child) as (app, store):
            assert app.send("delegate") == "parent finished"
            index = json.loads(index_for(app).read_text(encoding="utf-8"))
            assert len(index["agents"][0]["runs"]) == 2
            assert all(run["status"] == "completed" for run in index["agents"][0]["runs"])
            assert len(child_steps) == 3
            assert not any(
                "must not enter next run" in (message.get("content") or "")
                for body in child_steps
                for message in body["messages"]
            )
    finally:
        release.set()
        next_release.set()

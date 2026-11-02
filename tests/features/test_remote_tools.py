"""原模型工具循环加真实远端 HTTP，核对批准、来源、费用与恢复边界。"""

import pytest

from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.errors import ApprovalDeniedError
from adam.contracts.interop import RemoteAgentPolicy
from tests.features.test_browser_actions import application
from tests.features.test_interop_tasks import remote as remote
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import settings
from tests.features.test_tool_loop import answer, tool_response


def policy(client, **kwargs):
    return RemoteAgentPolicy(
        client.origin,
        kwargs.get("max_tasks", 3),
        kwargs.get("ceiling", 0),
        kwargs.get("budget", 0),
        "host verified local text callback; no paid model",
    )


def config(root):
    return settings(root, tool_discovery=ToolDiscoveryConfig(mode="full"))


def dialogue(body, number):
    if number == 1:
        return tool_response([call("submit", "remote_submit", local_id="job", text="hello")])
    if number == 2:
        return tool_response([call("query", "remote_query", local_id="job")])
    return answer("done")


def test_real_remote_through_original_loop_and_restart(tmp_path, remote):
    client, _, calls = remote
    approvals = []
    selected = policy(client)
    with application(
        tmp_path,
        dialogue,
        config(tmp_path),
        remote_policy=selected,
        confirm_remote=lambda plan: approvals.append(plan) or True,
    ) as (app, store, bodies):
        app.select_mode("act")
        app.send("delegate text")
        result = outputs(bodies[-1])[-1]
        assert result["remote_task"]["remote_id"]
        assert result["source"]["tool_name"] == "remote_query"
        assert result["cost"]["actual_microusd"] is None
        assert result["cost"]["contract"]["task_cost_ceiling_microusd"] == 0
        assert result["independently_verified"] is False
        sid = app.session.session_id
        assert approvals[0].session_id == sid and approvals[0].text == "hello"
        assert len(approvals) == 1 and calls == ["hello"]
        assert any(
            e["type"] == "tool_finished" and "remote_task" in e["payload"]["output"]
            for e in store.load_events(sid)
        )

    def resumed(body, number):
        if number == 1:
            return tool_response([call("again", "remote_submit", local_id="job", text="hello")])
        if number == 2:
            assert outputs(body)[-1]["ok"] is False
            return tool_response([call("original", "remote_query", local_id="job")])
        return answer("recovered")

    with application(
        tmp_path,
        resumed,
        config(tmp_path),
        remote_policy=selected,
        confirm_remote=lambda plan: approvals.append(plan) or True,
    ) as (app, _, bodies):
        app.resume(sid)
        app.select_mode("act")
        app.send("recover original")
        assert outputs(bodies[-1])[-1]["remote_task"]["state"] == "completed"
    assert calls == ["hello"] and len(approvals) == 1


@pytest.mark.parametrize("boundary", ["denied", "plan", "cost"])
def test_rejected_dispatch_has_no_remote_side_effect(tmp_path, remote, boundary):
    client, _, calls = remote
    approvals = []
    selected = policy(client, ceiling=1 if boundary == "cost" else 0)

    def one(body, number):
        return (
            tool_response([call("submit", "remote_submit", local_id="job", text="hello")])
            if number == 1
            else answer("done")
        )

    with application(
        tmp_path,
        one,
        config(tmp_path),
        remote_policy=selected,
        confirm_remote=lambda plan: approvals.append(plan) or boundary != "denied",
    ) as (app, _, bodies):
        app.select_mode("plan" if boundary == "plan" else "act")
        if boundary == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("test")
        else:
            app.send("test")
            assert outputs(bodies[-1])[-1]["ok"] is False
    assert calls == []
    assert len(approvals) == (1 if boundary == "denied" else 0)


def test_cross_session_query_and_task_limit(tmp_path, remote):
    client, _, calls = remote
    selected = policy(client, max_tasks=1)

    def two(body, number):
        if number <= 2:
            return tool_response(
                [call(f"s{number}", "remote_submit", local_id=f"j{number}", text="hello")]
            )
        return answer("done")

    with application(
        tmp_path, two, config(tmp_path), remote_policy=selected, confirm_remote=lambda plan: True
    ) as (app, _, bodies):
        app.select_mode("act")
        app.send("limited")
        assert outputs(bodies[-1])[-1]["ok"] is False
    assert calls == ["hello"]

    def query(body, number):
        return (
            tool_response([call("q", "remote_query", local_id="j1")])
            if number == 1
            else answer("done")
        )

    with application(
        tmp_path / "other",
        query,
        config(tmp_path / "other"),
        remote_policy=selected,
        confirm_remote=lambda plan: True,
    ) as (app, _, bodies):
        app.send("other session")
        assert outputs(bodies[-1])[-1]["ok"] is False


def test_remote_cancel_requires_separate_approval(tmp_path, remote):
    client, _, _ = remote
    approvals = []

    def respond(body, number):
        if number == 1:
            return tool_response([call("start", "remote_submit", local_id="slow", text="wait")])
        if number == 2:
            return tool_response([call("stop", "remote_cancel", local_id="slow")])
        return answer("cancel requested")

    with application(
        tmp_path,
        respond,
        config(tmp_path),
        remote_policy=policy(client),
        confirm_remote=lambda plan: approvals.append(plan) or True,
    ) as (app, _, bodies):
        app.select_mode("act")
        app.send("delegate then cancel")
        assert [p.operation for p in approvals] == ["remote_submit", "remote_cancel"]
        assert approvals[1].remote_id
        assert outputs(bodies[-1])[-1]["status"] in {"working", "canceled"}

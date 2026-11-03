"""B42.2：真实预算预留影响派发，低估不能绕过实际请求原子准入。"""

import json

import httpx
import pytest

from adam.contracts.errors import BudgetError
from tests.features.test_subagents import application, call
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("mode", ["reserved", "underestimate", "unknown"])
def test_dispatch_rechecks_real_reservations_and_hard_admission(tmp_path, mode):
    (tmp_path / "a.txt").write_text("evidence")
    parent_calls = child_calls = 0
    result = {}

    def child(request):
        nonlocal child_calls
        child_calls += 1
        return httpx.Response(200, json=answer("child result"))

    def parent(request):
        nonlocal parent_calls
        parent_calls += 1
        if parent_calls == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [call("dispatch", "spawn_agent", name="worker", task="read a.txt")]
                ),
            )
        return httpx.Response(200, json=answer("bounded dispatch finished"))

    def dispatch(manager, name, task):
        # 经正式宿主策略入口，在真实请求结算后、工具阶段运行。
        assert manager.recommend_delegation(2, 1000, 1000)["action"] == "delegate"
        tasks = (("worker", "read a.txt and report"),)
        if mode == "unknown":
            # 注入另一个成员已无法结算的消费；派发不能把它当成零。
            manager._budget.update("unresolved-member", 0, True)
            result.update(
                manager.dispatch_budgeted(
                    tasks, independent_tasks=2, child_estimate=1000, main_estimate=1000
                )
            )
            assert result["initial"]["action"] == "stop"
        else:
            remaining = manager.account(app.session.session_id).snapshot().remaining
            # 真实预留占用；留100 token验证估算过低仍被子请求的原准入检查拦下。
            with manager._budget.reserve(remaining - 100):
                estimate = 10 if mode == "underestimate" else 1000
                result.update(
                    manager.dispatch_budgeted(
                        tasks, independent_tasks=2, child_estimate=estimate, main_estimate=estimate
                    )
                )
                if mode == "reserved":
                    assert result["initial"]["action"] == "stop"
                else:
                    assert len(result["spawned"]) == 1
                    assert manager.wait([], 5)["agents"][0]["status"] == "failed"
            assert manager.account(app.session.session_id).snapshot().reserved >= 0
        assert child_calls == 0
        return result

    with application(tmp_path, parent, child, delegation_dispatch=dispatch) as (app, store):
        try:
            app.send("consider bounded delegation")
        except BudgetError:
            assert mode == "unknown"
        path = (
            store.root
            / "_subagents"
            / app.session.session_id
            / "dispatches"
            / (result["dispatch_id"] + ".json")
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == result
        assert saved["source"]["request_id"]
        assert not result["spawned"] if mode != "underestimate" else len(result["spawned"]) == 1

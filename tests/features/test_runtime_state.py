"""B04.1：从真实请求策略、共享预留与失败检测构造只读状态，不调用远端模型。"""

import json
from dataclasses import FrozenInstanceError

import pytest

from adam.application.request_policy import RequestPolicy
from adam.application.subagents.budget import SharedTokenBudget
from adam.config.settings import NoProgressConfig
from adam.contracts.errors import BudgetError, RequestNotSentError
from adam.contracts.execution import ExecutionMode
from adam.contracts.runtime import CountState, FailureState, RuntimeState, TokenState
from adam.tool_calls.progress import NoProgressGuard


def test_retry_count_is_physical_and_reading_state_does_not_consume_next_request():
    policy = RequestPolicy(3, lambda _: None)
    attempts = []

    def operation():
        attempts.append(policy.snapshot())
        if len(attempts) < 3:
            raise RequestNotSentError()
        return "ok"

    initial = policy.snapshot()
    assert initial.remaining == 3
    assert policy.run(operation) == "ok"
    assert [state.used for state in attempts] == [1, 2, 3]
    assert [state.remaining for state in attempts] == [2, 1, 0]
    assert initial.used == 0 and policy.snapshot().remaining == 0
    with pytest.raises(BudgetError):
        policy.run(operation)
    assert len(attempts) == 3


def test_shared_snapshot_keeps_settled_usage_reservations_and_unknown_distinct():
    budget = SharedTokenBudget(1000)
    budget.update("parent", 100, False)
    budget.update("child", 50, False)
    before = budget.snapshot()
    with budget.reserve(200):
        with budget.reserve(300):
            state = budget.snapshot()
            assert (state.known_used, state.reserved, state.remaining) == (150, 500, 350)
            assert budget.snapshot() == state  # 读取不能增加预留。
        assert budget.snapshot().reserved == 200
        budget.update("child", 75, True)
        unknown = budget.snapshot()
        assert unknown.known_used == 175 and unknown.reserved == 200
        assert unknown.unknown_usage and unknown.remaining is None
    assert budget.snapshot().reserved == 0
    assert budget.usage() == (175, True)
    with pytest.raises(BudgetError):
        with budget.reserve(1):
            pytest.fail("未知消费允许了新请求")
    assert before.remaining == 850 and not before.unknown_usage


def test_snapshot_is_not_a_reservation_or_permission_to_start_more_work():
    budget = SharedTokenBudget(100)
    old = budget.snapshot()
    with budget.reserve(90):
        assert old.remaining == 100 and budget.snapshot().remaining == 10
        with pytest.raises(BudgetError):
            with budget.reserve(old.remaining):
                pytest.fail("旧快照绕过了原子预留检查")
    budget.update("parent", 110, False)
    assert budget.snapshot().known_used == 110 and budget.snapshot().remaining == 0


def test_failure_evidence_tracks_repeats_and_reset_without_claiming_task_success():
    guard = NoProgressGuard(NoProgressConfig(enabled=True))
    failure = {"ok": False, "error": "文件不存在"}
    guard.observe("read_file", '{"path":"missing"}', failure)
    decision = guard.observe("read_file", '{"path":"missing"}', failure)
    state = FailureState(decision.consecutive_failures, decision.fingerprint)
    assert state.consecutive == 2 and state.fingerprint
    guard.observe("read_file", '{"path":"missing"}', failure, evidence_version="changed")
    assert guard.decision.consecutive_failures == 1
    assert guard.decision.fingerprint != state.fingerprint
    guard.observe("read_file", '{"path":"missing"}', {"ok": True})
    assert FailureState(guard.decision.consecutive_failures, guard.decision.fingerprint) == (
        FailureState(0)
    )
    assert state.consecutive == 2  # 历史快照不会被后续观察修改。


@pytest.mark.parametrize("pending", [None, (), ("child-a", "child-b")])
def test_aggregate_is_immutable_json_data_and_retains_unknown_vs_empty(tmp_path, pending):
    state = RuntimeState(
        requests=CountState(8, 2),
        tools=CountState(16, 7),
        tokens=TokenState(50000, 1000, False, None),
        failures=FailureState(),
        workspace=str(tmp_path),
        mode=ExecutionMode.PLAN,
        pending_agents=pending,
    )
    record = json.loads(json.dumps(state.as_record()))
    assert record["tokens"]["known_used"] == 1000
    assert record["tokens"]["reserved"] is None and record["tokens"]["remaining"] is None
    assert record["pending_agents"] == (list(pending) if pending is not None else None)
    assert record["environment"] == {"workspace": str(tmp_path), "mode": "plan"}
    record["requests"]["used"] = 99
    if record["pending_agents"] is not None:
        record["pending_agents"].append("forged")
    assert state.requests.used == 2 and state.pending_agents == pending
    with pytest.raises(FrozenInstanceError):
        state.mode = ExecutionMode.ACT
    assert list(tmp_path.iterdir()) == []

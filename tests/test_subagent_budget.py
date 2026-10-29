"""共享消费的并发准入边界；不把估算当成精确 API 账单。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from adam.application.subagents.budget import SharedTokenBudget
from adam.config.settings import SubagentConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import BudgetError, ConfigError, FailureCode, RequestInterrupted


def test_parallel_admission_cannot_spend_same_remaining_tokens():
    budget = SharedTokenBudget(100)
    ready = Barrier(2)
    admitted = Barrier(2)

    def run():
        ready.wait(timeout=3)
        try:
            with budget.reserve(70):
                admitted.wait(timeout=3)
                return True
        except BudgetError as error:
            assert error.code == FailureCode.SESSION_TOKEN_LIMIT
            admitted.wait(timeout=3)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(run), pool.submit(run)
        assert sorted([first.result(), second.result()]) == [False, True]
    with budget.reserve(100):
        assert budget.usage() == (0, False)


def test_settlement_replaces_snapshots_and_unknown_blocks_new_requests():
    budget = SharedTokenBudget(100)
    budget.update("parent", 10, False)
    with budget.reserve(50):
        assert budget.usage() == (10, False)
        budget.update("child", 20, False)
        budget.update("child", 25, False)
    assert budget.usage() == (35, False)
    budget.update("child", 25, True)
    with pytest.raises(BudgetError) as caught:
        with budget.reserve(1):
            pytest.fail("unknown usage must block admission")
    assert caught.value.code == FailureCode.USAGE_UNKNOWN
    assert budget.usage() == (35, True)


def test_reservation_released_after_failure_without_erasing_usage():
    budget = SharedTokenBudget(100)
    with pytest.raises(RuntimeError), budget.reserve(80):
        budget.update("child", 30, False)
        raise RuntimeError("simulated completed request followed by local failure")
    with budget.reserve(70):
        pass
    with pytest.raises(BudgetError), budget.reserve(71):
        pass


def test_cancellation_tokens_are_independent():
    first, second = CancellationToken(), CancellationToken()
    first.cancel()
    with pytest.raises(RequestInterrupted):
        first.wait(10)
    second.check()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 33])
def test_invalid_subagent_limits_rejected(value):
    with pytest.raises(ConfigError):
        SubagentConfig(max_concurrent=value)
    with pytest.raises(ConfigError):
        SubagentConfig(max_created_per_turn=value)

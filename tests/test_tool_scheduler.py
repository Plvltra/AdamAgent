"""可信并发声明及配置边界，不让模型数据开启并发。"""

import pytest

from adam.contracts.execution import ToolCapability
from adam.tool_calls.scheduler import ToolScheduler, ToolTask


@pytest.mark.parametrize("limit", [0, 3, -1, True, 1.5, "2"])
def test_concurrency_limit_is_one_or_two(limit):
    with pytest.raises(ValueError):
        ToolScheduler(max_concurrent=limit)


@pytest.mark.parametrize("seconds", [0, -1, 61, float("inf"), float("nan"), True, "1"])
def test_cleanup_deadline_is_positive_finite_and_bounded(seconds):
    with pytest.raises(ValueError):
        ToolScheduler(cleanup_timeout_seconds=seconds)


@pytest.mark.parametrize(
    "capability,parallel",
    [(cap, True) for cap in ToolCapability if cap is not ToolCapability.READ_ONLY]
    + [("read_only", True), (ToolCapability.READ_ONLY, 1)],
)
def test_only_trusted_read_only_capability_can_opt_in(capability, parallel):
    with pytest.raises(ValueError):
        ToolTask("a", lambda: lambda _: None, capability, parallel)


def test_duplicate_ids_fail_before_any_preparation():
    prepared = []

    def prepare():
        prepared.append("unexpected")
        return lambda _: None

    with pytest.raises(ValueError, match="重复"):
        ToolScheduler().run([ToolTask("a", prepare), ToolTask("a", prepare)])
    assert prepared == []


def test_empty_batch_has_no_result_callbacks():
    assert ToolScheduler().run([], on_result=lambda _: pytest.fail("unexpected")) == []

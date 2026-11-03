"""B42.1：预算与待回收状态驱动有界建议，不把未知用量当成免费。"""

import pytest

from adam.application.subagents.delegation_policy import choose_delegation
from adam.contracts.runtime import TokenState


@pytest.mark.parametrize(
    "tasks,used,reserved,unknown,active,pending,expected,count",
    [
        (1, 0, 0, False, 0, 0, "self", 0),
        (4, 0, 0, False, 0, 0, "delegate", 2),
        (4, 0, 7000, False, 1, 0, "delegate", 1),
        (4, 0, 9000, False, 1, 0, "wait", 0),
        (4, 0, 0, True, 0, 0, "stop", 0),
        (4, 0, None, False, 0, 0, "stop", 0),
        (4, 0, 0, True, 0, 1, "collect", 0),
        (4, 9000, 0, False, 0, 0, "stop", 0),
    ],
)
def test_policy_preserves_main_budget_and_never_ignores_pending_results(
    tasks, used, reserved, unknown, active, pending, expected, count
):
    decision = choose_delegation(
        TokenState(10000, used, unknown, reserved),
        independent_tasks=tasks,
        active=active,
        uncollected=pending,
        free_creation_slots=4,
        max_concurrent=2,
        child_estimate=1000,
        main_estimate=2000,
    )
    assert decision.action == expected and decision.children == count
    assert decision.reason

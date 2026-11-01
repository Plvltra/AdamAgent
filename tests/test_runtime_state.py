"""状态契约拒绝歧义计数；未知与实际超限均不被截成正常可用额度。"""

from dataclasses import replace

import pytest

from adam.contracts.execution import ExecutionMode
from adam.contracts.runtime import CountState, FailureState, RuntimeState, TokenState


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_invalid_counts_are_not_coerced(value):
    with pytest.raises(ValueError):
        CountState(8, value)
    with pytest.raises(ValueError):
        TokenState(100, 10, False, value)
    with pytest.raises(ValueError):
        FailureState(value)


def test_unknown_and_overspent_values_keep_their_original_evidence():
    assert CountState(8, 10).as_record() == {"maximum": 8, "used": 10, "remaining": 0}
    assert TokenState(100, 130, False, 20).remaining == 0
    assert TokenState(100, 20, True, 0).remaining is None
    assert TokenState(100, 20, False, None).remaining is None
    with pytest.raises(ValueError):
        TokenState(100, 10, 0, 0)
    with pytest.raises(ValueError):
        FailureState(1, "unverified model text")
    with pytest.raises(ValueError):
        FailureState(None, "a" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"workspace": "relative"},
        {"mode": "act"},
        {"pending_agents": ["child"]},
        {"pending_agents": ("child", "child")},
        {"pending_agents": ("",)},
        {"requests": {"maximum": 8, "used": 0}},
    ],
)
def test_aggregate_requires_host_validated_values(tmp_path, change):
    state = RuntimeState(
        CountState(8, 0),
        CountState(16, 0),
        TokenState(100, 0, False, 0),
        FailureState(),
        str(tmp_path),
        ExecutionMode.ACT,
        (),
    )
    with pytest.raises(ValueError):
        replace(state, **change)

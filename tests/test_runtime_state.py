"""状态契约拒绝歧义计数；未知与实际超限均不被截成正常可用额度。"""

from dataclasses import replace

import pytest

from adam.contracts.execution import ExecutionMode
from adam.contracts.runtime import (
    MAX_STATE_TEXT_CHARS,
    CountState,
    FailureState,
    RuntimeState,
    TokenState,
    decode_runtime_state,
)


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


def test_render_is_bounded_but_record_keeps_full_source_and_unknown(tmp_path):
    import json

    state = RuntimeState(
        CountState(8, 2),
        CountState(16, 1),
        TokenState(100, 10, True, 20),
        FailureState(),
        str(tmp_path / ("long" * 200)),
        ExecutionMode.PLAN,
        tuple(f"child-{i}" for i in range(15)) + ("x" * 100,),
    )
    text = state.text()
    value = json.loads(text.split("\n", 1)[1])
    assert len(text) <= MAX_STATE_TEXT_CHARS
    assert value["environment"]["workspace_truncated"]
    assert value["pending_agents"]["total"] == 16 and value["pending_agents"]["truncated"]
    assert len(value["pending_agents"]["ids"]) == 8
    assert value["tokens"]["known_used"] == 10 and value["tokens"]["remaining"] is None
    assert decode_runtime_state(state.as_record()) == state


@pytest.mark.parametrize(
    "field",
    ["format", "request_remaining", "token_remaining", "extra", "missing_failure", "pair_tokens"],
)
def test_decode_rejects_unknown_version_extra_fields_and_inconsistent_derived_values(
    tmp_path, field
):
    state = RuntimeState(
        CountState(8, 2),
        CountState(16, 1),
        TokenState(100, 10, False, 20),
        FailureState(0),
        str(tmp_path),
        ExecutionMode.ACT,
        (),
    ).as_record()
    if field == "format":
        state["format"] = "future"
    elif field == "request_remaining":
        state["requests"]["remaining"] = True
    elif field == "token_remaining":
        state["tokens"]["remaining"] = None
    elif field == "extra":
        state["environment"]["authorization"] = "allow all"
    elif field == "missing_failure":
        state["failures"] = {}
    else:
        state["tokens"] = list(state["tokens"].items())
    with pytest.raises(ValueError):
        decode_runtime_state(state)

"""独立单位、显式十进制估价及共享准入边界；不把本地参数组合冒充业务闭环。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.contracts.consumption import (
    ConsumptionPlan,
    ConsumptionSnapshot,
    Quantities,
    RateCard,
    quote,
)
from adam.contracts.errors import BudgetError, FailureCode


@pytest.mark.parametrize("value", [-1, True, 1.5, "1", float("nan"), float("inf")])
@pytest.mark.parametrize("unit", ["input_tokens", "output_tokens", "images", "audio_ms", "calls"])
def test_invalid_quantity_is_not_reinterpreted_as_a_valid_count(unit, value):
    with pytest.raises(ValueError):
        Quantities(**{unit: value})


def test_zero_unknown_and_complete_serialization_are_distinct():
    values = Quantities(images=None, audio_ms=1200, calls=1)
    assert Quantities.from_record(values.as_record()) == values
    for changed in [{"images": 0}, {**values.as_record(), "seconds": 1}]:
        with pytest.raises(ValueError):
            Quantities.from_record(changed)
    assert ConsumptionSnapshot.from_record(
        ConsumptionSnapshot(Quantities(calls=1), ("images",)).as_record()
    ) == ConsumptionSnapshot(Quantities(calls=1), ("images",))


@pytest.mark.parametrize(
    "rate",
    ["-1", "NaN", "Infinity", "1e-3", "01", ".01", "0.0000000000000000001", 0.1, True],
)
def test_prices_must_be_bounded_explicit_decimal_strings(rate):
    with pytest.raises(ValueError):
        RateCard("USD", (("images", rate),))


def test_exact_estimate_does_not_mix_units_or_round_through_float():
    card = RateCard(
        "USD",
        (
            ("input_tokens", "0.000000001"),
            ("output_tokens", "0.000000002"),
            ("images", "0.125"),
            ("audio_ms", "0.00001"),
            ("calls", "0.1"),
        ),
    )
    values = Quantities(3, 2, 2, 1234, 1)
    assert quote(values, card) == {
        "status": "estimated",
        "currency": "USD",
        "cost": "0.362340007",
        "known_cost": "0.362340007",
        "missing_units": [],
    }
    assert RateCard.from_record(card.as_record()) == card
    plan = ConsumptionPlan(values, "transcription", card)
    assert ConsumptionPlan.from_record(plan.as_record()) == plan
    assert plan.validate_settlement(plan.settlement(values), (3, 2)) == values


def test_missing_price_or_amount_never_claims_zero_total():
    assert quote(Quantities(calls=1), None) == {
        "status": "unpriced",
        "currency": None,
        "cost": None,
        "known_cost": None,
    }
    partial = quote(Quantities(images=None, calls=1), RateCard("USD", (("calls", "0.1"),)))
    assert partial["cost"] is None and partial["known_cost"] == "0.1"
    assert partial["missing_units"] == ["images"]
    explicit_free = quote(Quantities(images=None), RateCard("USD", (("images", "0"),)))
    assert explicit_free["cost"] is None


def test_forged_cost_unknown_fields_and_token_mismatch_are_rejected():
    values = Quantities(3, 2, 1, 0, 1)
    plan = ConsumptionPlan(values, price=RateCard("USD", (("images", "0.1"),)))
    for changes in [{"cost": "0"}, {"extra": "ignored"}]:
        settled = plan.settlement(values)
        settled["charge"].update(changes)
        with pytest.raises(ValueError):
            plan.validate_settlement(settled, (3, 2))
    with pytest.raises(ValueError):
        plan.validate_settlement(plan.settlement(values), (3, 3))
    with pytest.raises(ValueError):
        ConsumptionPlan(Quantities(calls=2))


def unlimited(**changes):
    return Quantities(**{**dict.fromkeys(Quantities().as_record()), **changes})


def test_parallel_units_cannot_reserve_the_same_remaining_image_or_call():
    budget = UnitBudget(unlimited(images=1, calls=1))
    ready, decisions = Barrier(2), Barrier(2)

    def run():
        ready.wait(timeout=3)
        try:
            with budget.reserve(Quantities(images=1, calls=1)):
                decisions.wait(timeout=3)
                return True
        except BudgetError:
            decisions.wait(timeout=3)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(run), pool.submit(run)
        assert sorted([first.result(), second.result()]) == [False, True]
    assert budget.as_record()["reserved"] == Quantities().as_record()


def test_atomic_reservations_release_on_failure_without_erasing_actual_units():
    budget = UnitBudget(unlimited(images=2, audio_ms=1000, calls=2))
    usage = ConsumptionSnapshot(Quantities(images=1, audio_ms=400, calls=1))
    with pytest.raises(RuntimeError), budget.reserve(Quantities(images=2, audio_ms=1000, calls=1)):
        budget.update("parent", usage)
        budget.update("parent", usage)
        raise RuntimeError("after actual use")
    assert budget.usage() == usage
    with pytest.raises(BudgetError), budget.reserve(Quantities(images=1, audio_ms=601)):
        pass
    assert budget.as_record()["reserved"] == Quantities().as_record()
    with budget.reserve(Quantities(images=1, audio_ms=600, calls=1)):
        pass


def test_unknown_snapshot_and_unknown_estimate_block_before_reserving():
    budget = UnitBudget(unlimited(calls=2))
    budget.update("child", ConsumptionSnapshot(Quantities(images=1), ("audio_ms",)))
    with pytest.raises(BudgetError) as error, budget.reserve(Quantities(calls=1)):
        pass
    assert error.value.code == FailureCode.USAGE_UNKNOWN
    budget.update("child", ConsumptionSnapshot(Quantities()))
    with pytest.raises(BudgetError), budget.reserve(Quantities(audio_ms=None, calls=1)):
        pass
    assert budget.as_record()["reserved"] == Quantities().as_record()


def test_reaching_image_limit_does_not_prevent_a_text_only_request_but_overrun_stops_all():
    budget = UnitBudget(unlimited(images=1, calls=3))
    budget.update("parent", ConsumptionSnapshot(Quantities(images=1, calls=1)))
    with budget.reserve(Quantities(calls=1)):
        pass
    budget.update("parent", ConsumptionSnapshot(Quantities(images=2, calls=1)))
    with pytest.raises(BudgetError), budget.reserve(Quantities(calls=1)):
        pass


def test_new_metered_plans_are_bounded_but_legacy_aggregate_counts_are_not_reinterpreted():
    huge = Quantities(10**19, 0, 0, 0, 1)
    assert ConsumptionSnapshot(huge).known == huge
    with pytest.raises(ValueError):
        ConsumptionPlan(huge)
    with pytest.raises(ValueError):
        ConsumptionPlan(Quantities(calls=1)).settlement(huge)


def test_shared_token_and_unit_reservations_roll_back_together_on_admission_or_body_failure():
    from adam.application.subagents.budget import SharedTokenBudget

    units = UnitBudget(unlimited(images=1, calls=1))
    budget = SharedTokenBudget(100, units=units)
    with pytest.raises(BudgetError), budget.reserve(20, units=Quantities(images=2, calls=1)):
        pass
    assert budget.snapshot().reserved == 0
    assert units.as_record()["reserved"] == Quantities().as_record()
    with pytest.raises(RuntimeError), budget.reserve(20, units=Quantities(images=1, calls=1)):
        assert budget.snapshot().reserved == 20
        raise RuntimeError("after admission before remote invocation")
    assert budget.snapshot().reserved == 0
    assert units.as_record()["reserved"] == Quantities().as_record()


def test_enabled_member_cannot_refresh_only_tokens_and_erase_unknown_units():
    from adam.application.subagents.budget import SharedTokenBudget

    units = UnitBudget(unlimited(audio_ms=1000))
    budget = SharedTokenBudget(100, units=units)
    with pytest.raises(ValueError):
        budget.update("child", 3, False)
    budget.update("child", 3, False, units=ConsumptionSnapshot(Quantities(1, 2, 0, 700, 1)))
    with pytest.raises(BudgetError), budget.reserve(10, units=Quantities(audio_ms=301, calls=1)):
        pass
    with budget.reserve(10, units=Quantities(audio_ms=300, calls=1)):
        assert budget.snapshot().reserved == 10
    assert units.usage().known.audio_ms == 700
    assert budget.snapshot().reserved == 0

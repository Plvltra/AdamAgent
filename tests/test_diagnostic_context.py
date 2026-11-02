"""B20.2.1：有界诊断观察的严格契约，真实文件和检查器场景放在 features。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from adam.config.settings import ContextConfig
from adam.context.builder import ContextBuilder, estimate_json_tokens
from adam.contracts.diagnostic_context import (
    MAX_CONTEXT_BYTES,
    DiagnosticObservation,
    DiagnosticSelection,
    decode_selection,
    selection_bytes,
    validate_sources,
)
from adam.contracts.diagnostics import parse_diagnostics
from adam.contracts.errors import BudgetError
from adam.contracts.history import HistoryTurn
from tests.test_diagnostics import report


def selection():
    return DiagnosticSelection(
        "s1", "t1", (DiagnosticObservation("h1", "c1", parse_diagnostics(report()), "current"),)
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", "future"),
        ("session_id", "s\n1"),
        ("turn_id", ""),
        ("omitted_count", True),
        ("omitted_count", -1),
        ("omitted_count", 1000001),
        ("extra", True),
    ],
)
def test_rejects_unknown_layout_or_unbounded_identity_and_count(key, value):
    with pytest.raises(ValueError):
        decode_selection({**selection().to_record(), key: value})


@pytest.mark.parametrize("freshness", [None, "passed", "not_checked"])
def test_checkable_reports_require_a_current_file_observation(freshness):
    value = selection().to_record()
    value["reports"][0]["freshness"] = freshness
    with pytest.raises(ValueError):
        decode_selection(value)


def test_old_items_remain_in_evidence_but_are_absent_from_model_observation():
    original = selection()
    for freshness in ("changed", "unavailable"):
        changed = replace(original, reports=(replace(original.reports[0], freshness=freshness),))
        evidence = changed.to_record()
        assert "invalid syntax" not in changed.messages()[0]["content"]
        assert changed.to_record() == evidence
        assert evidence["reports"][0]["report"]["items"][0]["message"] == "invalid syntax"
    assert "invalid syntax" in original.messages()[0]["content"]
    assert decode_selection(original.to_record()) == original
    assert DiagnosticSelection("s1", "t1").messages() == []


def test_duplicate_file_hook_identity_and_total_report_count_are_rejected():
    value = selection().to_record()
    other = deepcopy(value["reports"][0])
    value["reports"].append(other)
    with pytest.raises(ValueError):
        decode_selection(value)
    other["hook_id"] = "h2"
    with pytest.raises(ValueError):
        decode_selection(value)
    other["report"]["path"] = "other.py"
    other["hook_id"] = "h1"
    with pytest.raises(ValueError):
        decode_selection(value)
    value["reports"] *= 3
    with pytest.raises(ValueError):
        decode_selection(value)


def test_selection_capacity_counts_utf8_bytes_across_individually_valid_reports():
    payload = report()
    payload["items"][0]["message"] = "字" * 500
    payload["items"] *= 6
    first = parse_diagnostics(payload)
    second = replace(first, path="second.py")
    value = DiagnosticSelection(
        "s1",
        "t1",
        (
            DiagnosticObservation("h1", "c1", first, "current"),
            DiagnosticObservation("h2", "c2", second, "current"),
        ),
    ).to_record()
    assert selection_bytes(value) > MAX_CONTEXT_BYTES
    with pytest.raises(ValueError, match="exceeds"):
        decode_selection(value)


def test_selection_must_reference_latest_report_in_the_requested_turn():
    selected = selection()
    source = {
        "hook_id": "h1",
        "tool_call_id": "c1",
        "turn_id": "t1",
        "status": "observed",
        "event": "after_tool",
        "diagnostics": report(),
    }
    validate_sources(selected, [source])
    for changes in ({"hook_id": "h2"}, {"tool_call_id": "c2"}, {"turn_id": "t2"}):
        with pytest.raises(ValueError):
            validate_sources(selected, [{**source, **changes}])
    with pytest.raises(ValueError):
        validate_sources(replace(selected, omitted_count=1), [source])
    with pytest.raises(ValueError):
        validate_sources(selected, [source, {**source, "hook_id": "new"}])
    changed = {**report(), "checker": "other"}
    with pytest.raises(ValueError):
        validate_sources(selected, [{**source, "diagnostics": changed}])


@pytest.mark.parametrize("status", ["stale", "unavailable"])
def test_uncheckable_report_never_claims_current_diagnostics(status):
    value = selection().to_record()
    observation = value["reports"][0]
    observation["report"].update(
        status=status, checked_version="b" * 64 if status == "stale" else None, items=[]
    )
    with pytest.raises(ValueError):
        decode_selection(value)
    observation["freshness"] = "not_checked"
    assert decode_selection(value).reports[0].freshness == "not_checked"


def test_diagnostics_are_separate_protected_budget_and_only_whole_old_history_is_removed():
    current = [{"role": "user", "content": "edit"}]
    previous = HistoryTurn(
        "old",
        [{"role": "user", "content": "previous"}, {"role": "assistant", "content": "response"}],
    )
    diagnostics = selection()
    arguments = dict(
        model="test",
        system_prompt="system",
        history=[previous],
        current_turn=current,
        tools=[],
        diagnostics=diagnostics,
    )
    limits = ContextConfig(safety_margin_tokens=0, retain_recent_turns=0, prune_tool_results=False)
    full = ContextBuilder(limits, 4096).build(**arguments)
    assert full.estimate.diagnostics_tokens == sum(
        estimate_json_tokens(m) for m in diagnostics.messages()
    )
    assert full.messages[-2:] == current + diagnostics.messages()
    capacity = full.estimate.input_tokens - full.estimate.history_tokens
    exact = ContextBuilder(replace(limits, max_input_tokens=capacity), 4096).build(**arguments)
    exact.ensure_within_budget()
    assert exact.selected_turn_ids == () and exact.omitted_turn_ids == ("old",)
    assert exact.messages[-2:] == current + diagnostics.messages()
    assert exact.selection_record()["diagnostics"] == diagnostics.to_record()
    with pytest.raises(BudgetError):
        replace(
            exact, limits=replace(exact.limits, max_input_tokens=capacity - 1)
        ).ensure_within_budget()
    assert current == [{"role": "user", "content": "edit"}]


def test_old_layout_rejects_new_diagnostic_observations():
    with pytest.raises(ValueError, match="当前上下文布局"):
        ContextBuilder(ContextConfig(), 4096, layout_version=None).build(
            model="test",
            system_prompt="",
            history=[],
            current_turn=[],
            tools=[],
            diagnostics=selection(),
        )

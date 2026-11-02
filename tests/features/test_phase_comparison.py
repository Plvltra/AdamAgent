"""B34.3：固定任务重复对照和缺失/重叠计量，不根据模拟延时宣称模型更快。"""

import json

import httpx
import pytest

from scripts.phase_report import summarize
from scripts.task_eval import run_suite
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer


def test_repeated_trials_preserve_real_log_cache_fields_and_unknown_prices(tmp_path):
    def transport(task, name, iteration):
        payload = answer(json.dumps(task.expected))
        if name == "observed":
            payload["usage"].update(prompt_cache_hit_tokens=4, prompt_cache_miss_tokens=6)
        return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    base = config(tmp_path)
    result = run_suite(
        tmp_path / "runs",
        {"observed": base, "unknown": base},
        repeats=2,
        cases=("no-tools",),
        transport_factory=transport,
    )
    assert result["passed"] == 4
    prices = {
        "source": "synthetic test",
        "as_of": "2026-11-03",
        "model": "test",
        "currency": "CNY",
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_input_per_million": 0.5,
    }
    groups = {
        name: summarize([r for r in result["runs"] if r["config"] == name], prices)
        for name in ("observed", "unknown")
    }
    assert groups["observed"]["phases"]["model"]["samples"] == 2
    assert groups["observed"]["estimated_total_cost_cny"] == pytest.approx(24 / 1e6)
    assert groups["unknown"]["estimated_total_cost_cny"] is None
    assert all(r["cache_unknown_requests"] == 1 for r in groups["unknown"]["runs"])


def test_overlapping_phases_and_missing_records_are_not_added_or_zero_filled():
    run = {
        "phase_timing": {
            "records": [
                {"phase": "tool", "start_ms": 0, "end_ms": 100, "duration_ms": 100},
                {"phase": "approval", "start_ms": 10, "end_ms": 90, "duration_ms": 80},
                {"phase": "storage", "start_ms": 90, "end_ms": 100, "duration_ms": 10},
            ],
            "dropped": 1,
        }
    }
    report = summarize([run, {}])
    assert report["phases"]["tool"]["median_ms"] == 100
    assert report["phases"]["approval"]["median_ms"] == 80
    assert report["phases"]["tool"]["runs_without_records"] == 1
    assert report["phases"]["model"]["median_ms"] is None
    assert report["dropped_records"] == 1
    assert report["estimated_total_cost_cny"] is None
    assert "duration_ms" not in report

"""B34.2：阶段分布、缓存价格依据与离线CLI。"""

import json

import pytest

from scripts.phase_report import main, summarize


def test_report_distributions_cache_prices_and_cli(tmp_path, capsys):
    prices = {
        "source": "test-price-card",
        "as_of": "2026-11-03",
        "model": "test",
        "currency": "CNY",
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_input_per_million": 0.1,
    }
    run = {
        "run_id": "one",
        "model": "test",
        "unknown_usage": False,
        "request_observations": [
            {
                "usage": {"input_tokens": 100, "output_tokens": 10},
                "cache_usage": {
                    "format": "chat_completions_cache_v1",
                    "hit_tokens": 20,
                    "miss_tokens": 80,
                },
            }
        ],
        "phase_timing": {
            "records": [{"phase": "model", "duration_ms": v} for v in (10, 20, 30, None)]
        },
    }
    result = summarize([run], prices)
    assert result["phases"]["model"]["median_ms"] == 20
    assert result["phases"]["model"]["p95_ms"] == 30
    assert result["phases"]["model"]["unknown_records"] == 1
    assert result["phases"]["tool"]["median_ms"] is None
    assert result["estimated_total_cost_cny"] == pytest.approx(102 / 1e6)
    assert result["price_basis"] == prices
    report_path, prices_path = tmp_path / "report.json", tmp_path / "prices.json"
    report_path.write_text(json.dumps({"runs": [run]}))
    prices_path.write_text(json.dumps(prices))
    assert main([str(report_path), "--prices", str(prices_path)]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["cache_hit_tokens"] == 20
    run["request_observations"][0]["cache_usage"]["miss_tokens"] = None
    assert summarize([run], prices)["estimated_total_cost_cny"] is None
    with pytest.raises(ValueError, match="source"):
        summarize([run], {**prices, "source": ""})

"""B34离线阶段分布与显式价格估算；不请求模型，不把重叠阶段相加。"""

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from adam.contracts.usage import decode_cache_usage


def summarize(runs: list[dict[str, Any]], prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """汇总实际记录；缺观测计数独立保留，费用只覆盖已知请求和匹配模型。"""
    if prices is not None:
        for field in ("source", "as_of", "model"):
            if not isinstance(prices.get(field), str) or not prices[field].strip():
                raise ValueError("price source, date and model required")
        if prices.get("currency") != "CNY":
            raise ValueError("price currency must be CNY")
        for field in ("input_per_million", "output_per_million", "cached_input_per_million"):
            if not _number(prices.get(field)):
                raise ValueError("nonnegative finite prices required")
    phases = {}
    for phase in ("model", "first_output", "tool", "approval", "storage"):
        known, unknown, absent = [], 0, 0
        for run in runs:
            rows = [
                r for r in run.get("phase_timing", {}).get("records", []) if r["phase"] == phase
            ]
            absent += not rows
            for row in rows:
                value = row.get("duration_ms")
                if value is None:
                    unknown += 1
                elif not _number(value):
                    raise ValueError("invalid duration")
                else:
                    known.append(value)
        known.sort()
        phases[phase] = {
            "samples": len(known),
            "unknown_records": unknown,
            "runs_without_records": absent,
            "median_ms": statistics.median(known) if known else None,
            "p95_ms": known[math.ceil(len(known) * 0.95) - 1] if known else None,
        }
    costs = [_cost(run, prices) for run in runs]
    return {
        "phases": phases,
        "runs": costs,
        "price_basis": prices,
        "estimated_known_cost_cny": sum(r["known_cost_cny"] for r in costs),
        "estimated_total_cost_cny": sum(r["known_cost_cny"] for r in costs)
        if prices and all(r["complete"] for r in costs)
        else None,
        "dropped_records": sum(r.get("phase_timing", {}).get("dropped", 0) for r in runs),
        "scope": "inclusive phase distributions; not additive; estimates not invoices",
    }


def _cost(run: dict[str, Any], prices: dict[str, Any] | None) -> dict[str, Any]:
    """缓存字段必须成对且匹配输入总量；缺失时不倒算命中，不假设普通单价。"""
    known = 0.0
    requests = run.get("request_observations")
    complete = requests is not None and not run.get("unknown_usage", True)
    cache_hit = cache_miss = 0
    cache_unknown = 0
    for request in requests or []:
        usage = request.get("usage") or {}
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens)):
            complete = False
            cache_unknown += 1
            continue
        cache = decode_cache_usage(request.get("cache_usage"), input_tokens)
        if cache is None or cache.hit_tokens is None or cache.miss_tokens is None:
            cache_unknown += 1
            complete = False
            continue
        cache_hit += cache.hit_tokens
        cache_miss += cache.miss_tokens
        if prices is None or prices["model"] != run.get("model"):
            complete = False
            continue
        known += (
            cache.hit_tokens * prices["cached_input_per_million"]
            + cache.miss_tokens * prices["input_per_million"]
            + output_tokens * prices["output_per_million"]
        ) / 1e6
    return {
        "run_id": run.get("run_id"),
        "known_cost_cny": known,
        "complete": complete and prices is not None and prices["model"] == run.get("model"),
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_unknown_requests": cache_unknown,
    }


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def main(argv: list[str] | None = None) -> int:
    """读取B31 report.json和可选价格JSON，以JSON输出可追溯估算，不读取API配置。"""
    parser = argparse.ArgumentParser(description="离线阶段耗时及缓存费用报告")
    parser.add_argument("report", type=Path)
    parser.add_argument("--prices", type=Path)
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    prices = json.loads(args.prices.read_text(encoding="utf-8")) if args.prices else None
    print(json.dumps(summarize(report["runs"], prices), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""只读重建压缩对照与人工判读；输入、引用、消费和语义结论分别验证。"""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from adam.context.builder import estimate_json_tokens
from scripts.compaction_cases import CompactionCase, CompactionScenario, audit_summary, load_suite
from scripts.compaction_fixture import (
    FORMAT,
    LIMITS,
    digest,
    limits_record,
    mapped_case,
    mapped_scenario,
    schedule,
    turn_identity,
)

VERDICTS = frozenset({"preserved", "omitted", "contradicted", "unclear", "not_reviewed"})


def report_suite(directory: Path, *, review: Path | None = None) -> dict[str, Any]:
    """报告逐组输入、摘要、引用和成本；未运行、缺失、损坏及未知用量不混为零。

    review 是人工结论文件，必须绑定本次运行及摘要哈希；没有评审时不计算质量分。
    所有路径在指定目录内由序号生成，不跟随记录提供的文件路径，不发起模型请求。
    """
    manifest = _read(directory / "manifest.json")
    fields = {
        "format",
        "backend",
        "model",
        "suite_sha256",
        "selected",
        "schedule",
        "max_known_tokens",
        "limits",
        "input_hashes",
    }
    if not isinstance(manifest, dict) or manifest.keys() != fields or manifest["format"] != FORMAT:
        raise ValueError("未知评估清单。")
    if (
        manifest["backend"] not in {"mock", "live"}
        or not isinstance(manifest["model"], str)
        or not manifest["model"]
    ):
        raise ValueError("无效模型或评估来源。")
    if (
        type(manifest["max_known_tokens"]) is not int
        or not 1 <= manifest["max_known_tokens"] <= 1000000
    ):
        raise ValueError("无效额度。")
    suite = load_suite(directory / "cases.json")
    if suite.source_sha256 != manifest["suite_sha256"] or manifest["limits"] != limits_record():
        raise ValueError("样本或规划限额改变。")
    selected = manifest["selected"]
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise ValueError("无效查询列表。")
    runs = schedule(suite, tuple(selected))
    if (
        manifest["schedule"] != runs
        or not isinstance(manifest["input_hashes"], dict)
        or manifest["input_hashes"].keys() != {str(item["index"]) for item in runs}
    ):
        raise ValueError("运行顺序或输入哈希不完整。")
    rows = []
    for item in runs:
        case = next(case for case in suite.cases if case.case_id == item["case_id"])
        scenario = mapped_scenario(suite.scenario(case.scenario_id))
        case = mapped_case(case)
        path = directory / f"{item['index']:02d}.json"
        if not path.exists():
            rows.append({**item, "state": "missing", "usage": None, "audit": None})
            continue
        try:
            row = _read(path)
            if not isinstance(row, dict):
                raise ValueError("请求记录不是对象。")
            _validate_row(row, item, manifest, scenario, case)
            row["audit"] = (
                audit_summary(
                    row["summary"], scenario, case, covered_turn_ids=tuple(row["source_turn_ids"])
                )
                if row["summary"]
                else None
            )
            # 完整输入保留在原记录，报告只展示摘要和可定位证据，避免重复展开长历史。
            row.pop("request")
            row["record_file"] = path.name
            rows.append(row)
        except (ValueError, OSError, KeyError, TypeError):
            rows.append({**item, "state": "invalid", "usage": None, "audit": None})
    run_hash = digest(manifest)
    decisions = _reviews(review, run_hash, rows) if review is not None else {}
    for row in rows:
        row["review"] = decisions.get(row["index"], [])
    groups = {}
    for arm in ("generic", "focused"):
        members = [row for row in rows if row["arm"] == arm]
        groups[arm] = {
            "states": dict(Counter(row["state"] for row in members)),
            "known_tokens": sum(sum(row["usage"]) for row in members if row["usage"] is not None),
            "semantic_verdicts": dict(
                Counter(check["verdict"] for row in members for check in row["review"])
            ),
            "semantics_unreviewed": sum(
                row["state"] == "succeeded"
                and (
                    not row["review"]
                    or any(check["verdict"] == "not_reviewed" for check in row["review"])
                )
                for row in members
            ),
        }
    return {
        "format": FORMAT,
        "backend": manifest["backend"],
        "model": manifest["model"],
        "suite_sha256": suite.source_sha256,
        "run_sha256": run_hash,
        "known_tokens": sum(group["known_tokens"] for group in groups.values()),
        "usage_unknown": any(row["state"] != "planned" and row["usage"] is None for row in rows),
        "groups": groups,
        "requests": rows,
        "limitations": "仅对照同一资料的摘要保真，未验证后续任务成功率。引用/文字命中不是语义结论；人工评审也须区分模拟与真实模型。未运行或未知消费不补零。",
    }


def review_template(directory: Path) -> dict[str, Any]:
    """为成功摘要生成空白判读表；默认 not_reviewed，原文与期望在只读报告中查看。"""
    report = report_suite(directory)
    return {
        "format": "compaction_review_v1",
        "run_sha256": report["run_sha256"],
        "decisions": [
            {
                "index": row["index"],
                "case_id": row["case_id"],
                "arm": row["arm"],
                "summary_sha256": row["audit"]["summary_sha256"],
                "checks": [
                    {"key": check["key"], "verdict": "not_reviewed", "note": ""}
                    for check in row["audit"]["checks"]
                ],
            }
            for row in report["requests"]
            if row["state"] == "succeeded"
        ],
    }


def _read(path: Path) -> object:
    """有界读取单个证据文件，不修补损坏记录。"""
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("评估记录超过 2 MiB。")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("重复证据字段。")
        result[key] = value
    return result


def _validate_row(
    row: dict[str, Any],
    item: dict[str, Any],
    manifest: dict[str, Any],
    scenario: CompactionScenario,
    case: CompactionCase,
) -> None:
    """核对固定资料、实际覆盖和输入指纹；损坏资料不能继续获得成功或费用结论。"""
    fields = {
        "index",
        "case_id",
        "arm",
        "state",
        "scenario_id",
        "source_turn_ids",
        "estimated_input_tokens",
        "request",
        "input_sha256",
        "summary",
        "usage",
        "elapsed_ms",
        "error",
    }
    if (
        not isinstance(row, dict)
        or row.keys() != fields
        or type(row["index"]) is not int
        or any(row.get(key) != value for key, value in item.items())
    ):
        raise ValueError("请求记录来源不一致。")
    if row["scenario_id"] != scenario.scenario_id:
        raise ValueError("历史来源不一致。")
    request = row["request"]
    if not isinstance(request, dict) or request.keys() != {
        "messages",
        "model",
        "tools",
        "max_tokens",
    }:
        raise ValueError("请求字段无效。")
    if (
        digest(request) != row["input_sha256"]
        or row["input_sha256"] != manifest["input_hashes"][str(item["index"])]
    ):
        raise ValueError("请求输入指纹不一致。")
    if (
        request["model"] != manifest["model"]
        or request["tools"] != []
        or request["max_tokens"] != LIMITS.summary_max_output_tokens
    ):
        raise ValueError("模型或请求边界改变。")
    messages = request["messages"]
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or any(
            not isinstance(message, dict) or not isinstance(message.get("content"), str)
            for message in messages
        )
        or messages[0].get("role") != "system"
        or messages[1].get("role") != "user"
    ):
        raise ValueError("摘要输入排列无效。")
    material = json.loads(messages[1]["content"])
    sources = row["source_turn_ids"]
    if (
        not isinstance(sources, list)
        or not sources
        or sources != [turn.turn_id for turn in scenario.history[: len(sources)]]
        or len(sources) > len(scenario.history) - LIMITS.retain_recent_turns
    ):
        raise ValueError("覆盖范围不是允许的连续旧历史。")
    expected = {
        "session_constraints": scenario.constraints,
        "previous_summary": None,
        "turns": [
            {"turn_id": turn.turn_id, "messages": turn.messages}
            for turn in scenario.history[: len(sources)]
        ],
        "current_task": {
            "turn_id": turn_identity(case.scenario_id, "query-" + case.case_id),
            "messages": [{"role": "user", "content": case.query}],
        }
        if item["arm"] == "focused"
        else None,
        "previous_summary_id": None,
        "previous_covered_turn_ids": [],
        "source_positions": [
            {"turn_id": turn.turn_id, "message_count": len(turn.messages), "index_base": 0}
            for turn in scenario.history[: len(sources)]
        ],
    }
    if (
        material != expected
        or type(row["estimated_input_tokens"]) is not int
        or row["estimated_input_tokens"]
        != sum(estimate_json_tokens(message) for message in messages)
        or row["estimated_input_tokens"] + LIMITS.safety_margin_tokens > LIMITS.max_input_tokens
    ):
        raise ValueError("摘要素材或估算不一致。")
    usage = row["usage"]
    if usage is not None and (
        not isinstance(usage, list)
        or len(usage) != 2
        or any(type(value) is not int or value < 0 for value in usage)
    ):
        raise ValueError("无效实际用量。")
    if row["state"] in {"planned", "started"}:
        if any(row[key] is not None for key in ("summary", "usage", "elapsed_ms", "error")):
            raise ValueError("未结束记录包含结果。")
        return
    if (
        row["state"] not in {"succeeded", "failed", "interrupted"}
        or type(row["elapsed_ms"]) is not int
        or row["elapsed_ms"] < 0
    ):
        raise ValueError("请求终态无效。")
    if row["summary"] is not None and (
        not isinstance(row["summary"], str) or not row["summary"].strip()
    ):
        raise ValueError("无效摘要。")
    if row["state"] == "succeeded":
        if row["summary"] is None or usage is None or row["error"] is not None:
            raise ValueError("成功记录缺少已知结果。")
    elif not isinstance(row["error"], str) or not row["error"]:
        raise ValueError("失败原因缺失。")


def _reviews(
    path: Path, run_hash: str, rows: list[dict[str, Any]]
) -> dict[int, list[dict[str, str]]]:
    """只接收绑定本次成功摘要的人工判读；不为未评审、损坏或失败项补结论。"""
    data = _read(path)
    if (
        not isinstance(data, dict)
        or data.keys() != {"format", "run_sha256", "decisions"}
        or data["format"] != "compaction_review_v1"
        or data["run_sha256"] != run_hash
        or not isinstance(data["decisions"], list)
    ):
        raise ValueError("评审不属于本次运行。")
    decisions = {}
    for decision in data["decisions"]:
        if (
            not isinstance(decision, dict)
            or decision.keys() != {"index", "case_id", "arm", "summary_sha256", "checks"}
            or type(decision["index"]) is not int
            or decision["index"] in decisions
        ):
            raise ValueError("无效或重复评审。")
        row = next((row for row in rows if row["index"] == decision["index"]), None)
        if (
            row is None
            or row["state"] != "succeeded"
            or any(decision[key] != row[key] for key in ("case_id", "arm"))
            or decision["summary_sha256"] != row["audit"]["summary_sha256"]
        ):
            raise ValueError("评审摘要已经改变或未成功。")
        checks = decision["checks"]
        if not isinstance(checks, list) or len(checks) != len(row["audit"]["checks"]):
            raise ValueError("评审判据数量不匹配。")
        for check in checks:
            if (
                not isinstance(check, dict)
                or check.keys() != {"key", "verdict", "note"}
                or not isinstance(check["key"], str)
                or not isinstance(check["verdict"], str)
                or check["verdict"] not in VERDICTS
                or not isinstance(check["note"], str)
                or len(check["note"]) > 4000
            ):
                raise ValueError("无效人工判读。")
            if check["verdict"] != "not_reviewed" and not check["note"].strip():
                raise ValueError("人工结论需简述依据或遗漏。")
        if {check["key"] for check in checks} != {check["key"] for check in row["audit"]["checks"]}:
            raise ValueError("评审判据不匹配。")
        decisions[decision["index"]] = checks
    return decisions

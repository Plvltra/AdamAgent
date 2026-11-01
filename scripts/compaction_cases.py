"""B05 保真样本与引用审计：核对原文位置，语义是否正确留给显式评审。"""

import argparse
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.contracts.history import HistoryTurn
from adam.contracts.protocol import validate_turn

MAX_BYTES = 1024 * 1024
DIMENSIONS = frozenset({"fact", "cause", "constraint", "failure"})
REFERENCE = re.compile(r"([A-Za-z0-9_-]{1,64}):(0|[1-9][0-9]{0,5})\Z")
CITATIONS = re.compile(r"\[([^\[\]\n]*:[^\[\]\n]*)\]")


@dataclass(frozen=True)
class FidelityCheck:
    """人工判读的关键结论及逐字依据；quote 必须存在，statement 的含义不由子串判定。"""

    key: str
    dimension: str
    statement: str
    source_ref: str
    quote: str


@dataclass(frozen=True)
class CompactionCase:
    """同一历史可用于不同查询；checks 不发给摘要模型，避免泄漏判定答案。"""

    case_id: str
    scenario_id: str
    query: str
    checks: tuple[FidelityCheck, ...]


@dataclass(frozen=True)
class CompactionScenario:
    """完整合成成功轮次，包括工具失败的真实语义；不含可自动执行的任务。"""

    scenario_id: str
    constraints: str
    history: tuple[HistoryTurn, ...]


@dataclass(frozen=True)
class CompactionSuite:
    """版本化数据及源文件哈希；调用者修改消息副本不会改变磁盘样本。"""

    suite_id: str
    revision: int
    source_sha256: str
    scenarios: tuple[CompactionScenario, ...]
    cases: tuple[CompactionCase, ...]

    def scenario(self, identity: str) -> CompactionScenario:
        """按明确 ID 选择历史，不用名称近似匹配。"""
        result = next((item for item in self.scenarios if item.scenario_id == identity), None)
        if result is None:
            raise ValueError("不存在的历史 ID。")
        return result


def load_suite(path: Path) -> CompactionSuite:
    """读取不超过 1 MiB 的 JSON；非法协议、重复键或找不到的判据原文抛 ValueError。"""
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("摘要样本超过 1 MiB。")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("样本必须是有限层级的 UTF-8 JSON。") from error
    data = _object(value, {"schema_version", "suite_id", "revision", "scenarios", "cases"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("未知摘要样本版本。")
    if type(data["revision"]) is not int or data["revision"] < 1:
        raise ValueError("修订号必须为正整数。")
    scenarios = tuple(_scenario(item) for item in _items(data["scenarios"], 16))
    if len({item.scenario_id for item in scenarios}) != len(scenarios):
        raise ValueError("重复历史 ID。")
    cases = []
    for item in _items(data["cases"], 64):
        row = _object(item, {"case_id", "scenario_id", "query", "checks"})
        scenario_id = _text(row["scenario_id"])
        scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
        if scenario is None:
            raise ValueError("查询引用了不存在的历史。")
        checks = []
        for check in _items(row["checks"], 16):
            fields = _object(check, {"key", "dimension", "statement", "source_ref", "quote"})
            parsed = FidelityCheck(**{key: _text(value) for key, value in fields.items()})
            if parsed.dimension not in DIMENSIONS:
                raise ValueError("未知保真维度。")
            message = resolve_source(scenario.history, parsed.source_ref)
            content = message.get("content")
            if not isinstance(content, str) or parsed.quote not in content:
                raise ValueError("判据引用的逐字依据不在原文中。")
            checks.append(parsed)
        if len({check.key for check in checks}) != len(checks):
            raise ValueError("同一查询有重复判据 ID。")
        cases.append(
            CompactionCase(_text(row["case_id"]), scenario_id, _text(row["query"]), tuple(checks))
        )
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("重复查询 ID。")
    return CompactionSuite(
        _text(data["suite_id"]), data["revision"], sha256(raw).hexdigest(), scenarios, tuple(cases)
    )


def resolve_source(history: tuple[HistoryTurn, ...], reference: str) -> dict[str, Any]:
    """解析 turn_id:message_index，返回完整消息副本；错误引用不退回邻近消息或其他历史。"""
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError("malformed_reference")
    turn_id, index_text = match.groups()
    turn = next((item for item in history if item.turn_id == turn_id), None)
    if turn is None:
        raise ValueError("unknown_turn")
    index = int(index_text)
    if index >= len(turn.messages):
        raise ValueError("index_out_of_bounds")
    return deepcopy(turn.messages[index])


def audit_summary(
    summary: str,
    scenario: CompactionScenario,
    case: CompactionCase,
    *,
    covered_turn_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """报告引用可追溯性及待评审判据，不把引用存在或文字命中当作语义保真。

    covered_turn_ids 应取实际 Summary 的覆盖范围；缺省仅检查全历史能否定位，
    不声称这些资料实际进入了摘要请求。函数只读，不运行模型或写入会话。
    """
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode("utf-8")) > MAX_BYTES
    ):
        raise ValueError("摘要必须是非空、有界文本。")
    if case.scenario_id != scenario.scenario_id:
        raise ValueError("查询与历史不匹配。")
    known = {turn.turn_id for turn in scenario.history}
    if covered_turn_ids is not None and (
        len(set(covered_turn_ids)) != len(covered_turn_ids) or not set(covered_turn_ids) <= known
    ):
        raise ValueError("摘要覆盖范围不是该历史的唯一轮次。")
    references = list(dict.fromkeys(CITATIONS.findall(summary)))
    if len(references) > 128:
        raise ValueError("摘要引用超过 128 条。")
    resolutions = []
    valid = set()
    for reference in references:
        try:
            message = resolve_source(scenario.history, reference)
            if covered_turn_ids is not None and reference.split(":", 1)[0] not in covered_turn_ids:
                raise ValueError("outside_coverage")
        except ValueError as error:
            resolutions.append({"reference": reference, "resolvable": False, "reason": str(error)})
            continue
        encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
        resolutions.append(
            {
                "reference": reference,
                "resolvable": True,
                "role": message["role"],
                "message_sha256": sha256(encoded.encode("utf-8")).hexdigest(),
                "source_excerpt": encoded[:1500],
                "excerpt_truncated": len(encoded) > 1500,
            }
        )
        valid.add(reference)
    return {
        "format": "compaction_audit_v1",
        "case_id": case.case_id,
        "scenario_id": scenario.scenario_id,
        "summary_sha256": sha256(summary.encode("utf-8")).hexdigest(),
        "summary_chars": len(summary),
        "coverage_checked": covered_turn_ids is not None,
        "covered_turn_ids": list(covered_turn_ids) if covered_turn_ids is not None else None,
        "references": resolutions,
        "checks": [
            {
                "key": check.key,
                "dimension": check.dimension,
                "statement": check.statement,
                "source_ref": check.source_ref,
                "source_quote": check.quote,
                "reference_present": check.source_ref in valid,
                "literal_quote_present": check.quote in summary,
                "semantic_status": "not_reviewed",
            }
            for check in case.checks
        ],
        "semantic_status": "not_reviewed",
    }


def main(argv: list[str] | None = None) -> int:
    """列出查询和判据，或审计给定摘要；不创建输出文件或调用模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path, default=Path("examples/compaction-evaluation/cases-v1.json")
    )
    parser.add_argument("--case")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.cases)
        selected = [case for case in suite.cases if not args.case or case.case_id == args.case]
        if not selected:
            raise ValueError("不存在的 --case。")
        if args.summary:
            case = next((item for item in suite.cases if item.case_id == args.case), None)
            if case is None:
                raise ValueError("审计摘要必须指定存在的 --case。")
            with args.summary.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            result = audit_summary(raw.decode("utf-8"), suite.scenario(case.scenario_id), case)
        else:
            result = {
                "suite_id": suite.suite_id,
                "revision": suite.revision,
                "source_sha256": suite.source_sha256,
                "cases": [
                    {
                        "id": case.case_id,
                        "query": case.query,
                        "dimensions": sorted({check.dimension for check in case.checks}),
                    }
                    for case in selected
                ],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(2, f"样本或摘要无效：{error}\n")
    return 0


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("重复 JSON 字段。")
        result[key] = value
    return result


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != keys:
        raise ValueError("样本字段不匹配。")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise ValueError("样本文本必须非空且不超过 12000 字符。")
    return value


def _items(value: object, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError("样本列表长度无效。")
    return value


def _scenario(value: object) -> CompactionScenario:
    data = _object(value, {"scenario_id", "constraints", "turns"})
    history = []
    for item in _items(data["turns"], 128):
        row = _object(item, {"turn_id", "messages"})
        turn_id = _text(row["turn_id"])
        if REFERENCE.fullmatch(turn_id + ":0") is None:
            raise ValueError("历史轮次 ID 无法用于引用。")
        messages = _items(row["messages"], 128)
        if any(not isinstance(message, dict) for message in messages):
            raise ValueError("历史消息必须是对象。")
        validate_turn(messages)
        history.append(HistoryTurn(turn_id, messages))
    if len({turn.turn_id for turn in history}) != len(history):
        raise ValueError("重复历史轮次 ID。")
    return CompactionScenario(
        _text(data["scenario_id"]), _text(data["constraints"]), tuple(history)
    )


if __name__ == "__main__":
    raise SystemExit(main())

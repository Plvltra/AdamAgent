"""B33多维评分协议；调用方提供独立证据，模型自评仅作附注。"""

from dataclasses import dataclass
from typing import Any, Literal

Dimension = Literal["result", "process", "permission", "omission"]
DIMENSIONS = ("result", "process", "permission", "omission")


@dataclass(frozen=True)
class Criterion:
    """评分前由宿主确定的判据；passed表示满足该判据，不允许回答自行改写标准。"""

    key: str
    dimension: Dimension
    description: str


@dataclass(frozen=True)
class Evidence:
    """宿主绑定的判据证据；source为可追溯位置，origin不能从待评回答中读取。

    deterministic应来自实际验证器，human来自人工标注；model只是建议。
    本协议不认证文件真实性，调用方负责来源、版本和任务身份匹配。
    """

    criterion: str
    origin: Literal["deterministic", "human", "model"]
    passed: bool
    source: str
    detail: str = ""


def score(criteria: tuple[Criterion, ...], evidence: tuple[Evidence, ...]) -> dict[str, Any]:
    """逐判据评分，再按四维汇总；缺证据或同级冲突拒绝评分，不生成误导性总分。"""
    keys = [c.key for c in criteria]
    if len(set(keys)) != len(keys) or any(
        not c.key or not c.description or c.dimension not in DIMENSIONS for c in criteria
    ):
        raise ValueError("invalid or duplicate criterion")
    if any(
        e.criterion not in keys
        or e.origin not in {"deterministic", "human", "model"}
        or type(e.passed) is not bool
        for e in evidence
    ):
        raise ValueError("invalid evidence")
    rows = []
    for criterion in criteria:
        available = [e for e in evidence if e.criterion == criterion.key and e.source.strip()]
        selected = [e for e in available if e.origin == "deterministic"]
        if not selected:
            selected = [e for e in available if e.origin == "human"]
        values = {e.passed for e in selected}
        status = "abstain" if len(values) != 1 else ("pass" if values == {True} else "fail")
        rows.append(
            {
                "criterion": criterion.key,
                "dimension": criterion.dimension,
                "status": status,
                "reason": "conflicting_evidence"
                if len(values) > 1
                else "missing_independent_evidence"
                if not values
                else "supported",
                "sources": [e.source for e in selected],
                "model_suggestions": [
                    {"passed": e.passed, "source": e.source, "detail": e.detail}
                    for e in available
                    if e.origin == "model"
                ],
            }
        )
    dimensions = {}
    for dimension in DIMENSIONS:
        statuses = [r["status"] for r in rows if r["dimension"] == dimension]
        dimensions[dimension] = (
            "fail"
            if "fail" in statuses
            else "abstain"
            if not statuses or "abstain" in statuses
            else "pass"
        )
    return {
        "format": "adam_rubric_v1",
        "criteria": rows,
        "dimensions": dimensions,
        "all_criteria_met": bool(criteria) and all(r["status"] == "pass" for r in rows),
        "scope": "declared_criteria_only",
    }

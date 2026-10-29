"""固定题集的检索对照；只度量候选证据，不评价 LLM 文笔或凭空生成的答案。"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.documents.models import DocumentError
from adam.documents.search import SearchResult


def evaluate(
    search: Callable[[str, int], SearchResult], cases: object, limit: int = 3
) -> dict[str, Any]:
    """统计有答案题 Hit@k 和无答案题的空结果数，并保留逐题依据。

    cases 为带 id/kind/query/path/evidence 的对象数组；path 为期望来源的相对尾路径，
    evidence 是应包含的原文。无答案题的 path/evidence 均为 null。
    """
    validated = _validate_cases(cases)
    rows = []
    for case in validated:
        started = time.perf_counter()
        result = search(case["query"], limit)
        rank = next(
            (
                number
                for number, hit in enumerate(result.hits, 1)
                if case["path"] is not None
                and Path(hit.chunk.path).as_posix().endswith("/" + case["path"])
                and case["evidence"] in hit.chunk.text
            ),
            None,
        )
        rows.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "query": case["query"],
                "answerable": case["path"] is not None,
                "hit": rank is not None,
                "rank": rank,
                "empty": not result.hits,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "returned_chars": sum(len(hit.chunk.text) for hit in result.hits),
                "retrieved": [
                    {
                        "path": Path(hit.chunk.path).name,
                        "start_line": hit.chunk.start_line,
                        "end_line": hit.chunk.end_line,
                        "text": hit.chunk.text,
                        "score": round(hit.score, 6),
                    }
                    for hit in result.hits
                ],
            }
        )
    answerable = [row for row in rows if row["answerable"]]
    no_answer = [row for row in rows if not row["answerable"]]
    return {
        "k": limit,
        "answerable": len(answerable),
        "hits": sum(row["hit"] for row in answerable),
        "hit_at_k": sum(row["hit"] for row in answerable) / len(answerable) if answerable else None,
        "no_answer": len(no_answer),
        "no_answer_empty": sum(row["empty"] for row in no_answer),
        "query_time_ms": round(sum(row["elapsed_ms"] for row in rows), 3),
        "returned_chars": sum(row["returned_chars"] for row in rows),
        "rows": rows,
    }


def _validate_cases(cases: object) -> list[dict[str, Any]]:
    """拒绝残缺、重复或没有明确期望证据的题目，防止虚假的评估通过。"""
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise DocumentError("评估题集必须是 1～1000 项的 JSON 数组。")
    identifiers: set[str] = set()
    for item in cases:
        if (
            not isinstance(item, dict)
            or item.keys() != {"id", "kind", "query", "path", "evidence"}
            or any(
                not isinstance(item[key], str) or not item[key].strip()
                for key in ("id", "kind", "query")
            )
            or item["id"] in identifiers
            or item["kind"] not in {"answer", "ambiguous", "paraphrase", "stale", "no_answer"}
        ):
            raise DocumentError("评估题目字段、类别或 ID 无效。")
        identifiers.add(item["id"])
        if item["kind"] == "no_answer":
            valid = item["path"] is None and item["evidence"] is None
        else:
            valid = (
                isinstance(item["path"], str)
                and bool(item["path"].strip())
                and not Path(item["path"]).is_absolute()
                and ".." not in Path(item["path"]).parts
                and isinstance(item["evidence"], str)
                and bool(item["evidence"].strip())
            )
        if not valid:
            raise DocumentError("有答案题必须标注来源和原文；无答案题来源和原文必须为 null。")
    return cases

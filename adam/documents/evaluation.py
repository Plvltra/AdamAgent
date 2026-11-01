"""固定题集的检索对照；只度量候选证据，不评价 LLM 文笔或生成答案。"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.documents.models import Chunk, DocumentError
from adam.documents.search import SearchResult


def evaluate(
    search: Callable[[str, int], SearchResult], cases: object, limit: int = 3
) -> dict[str, Any]:
    """统计 Hit、MRR 和多相关片段 Recall@k，单独报告无答案题的空结果与误召回。

    兼容旧 path/evidence 单证据题；新 relevant 数组以路径、文件版本和分块行号绑定
    多个相关片段。全部题目先校验；来源版本不符不命中，查询间语料变化则拒绝整份报告。
    无答案题不进入有答案指标的分母；重复命中不能增加召回率。
    """
    if type(limit) is not int or not 1 <= limit <= 20:
        raise DocumentError("评估 k 必须是 1～20 的整数。")
    validated = _validate_cases(cases)
    rows = []
    generation = None
    context_generation = None
    for case in validated:
        started = time.perf_counter()
        result = search(case["query"], limit)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if generation is not None and result.generation != generation:
            raise DocumentError("评估期间语料发生变化；请固定资料后重新运行，未生成混合版本报告。")
        generation = result.generation
        current_context = (
            result.contextualization.notes_version if result.contextualization else None
        )
        if rows and current_context != context_generation:
            raise DocumentError("评估期间语境说明发生变化；未生成混合说明版本报告。")
        context_generation = current_context
        rows.append(_case_result(case, result, limit, elapsed))
    return {
        "format_version": 2,
        "cases_sha256": hashlib.sha256(
            json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source_generation": generation,
        "context_generation": context_generation,
        "k": limit,
        **_aggregate(rows),
        "by_kind": {
            kind: _aggregate([row for row in rows if row["kind"] == kind])
            for kind in sorted({row["kind"] for row in rows})
        },
        "rows": rows,
    }


def _case_result(
    case: dict[str, Any], result: SearchResult, limit: int, elapsed: float
) -> dict[str, Any]:
    """保留金标准与逐片段排名；拒绝超过 k 或重复来源的结果，避免指标暗中膨胀。"""
    hits = result.hits
    if len(hits) > limit or len({hit.chunk.chunk_id for hit in hits}) != len(hits):
        raise DocumentError("检索结果超过 k 或包含重复片段，不能生成有效评估。")
    sources = case["relevant"]
    matched: dict[int, int] = {}
    for rank, hit in enumerate(hits, 1):
        indices = [i for i, source in enumerate(sources) if _matches(source, hit.chunk)]
        if len(indices) > 1:
            raise DocumentError("一个检索片段匹配多个金标准；请为相关片段提供唯一位置。")
        for i in indices:
            matched.setdefault(i, rank)
    first = min(matched.values(), default=None)
    return {
        "id": case["id"],
        "kind": case["kind"],
        "query": case["query"],
        "method": result.method,
        "generation": result.generation,
        **({"fusion": asdict(result.fusion)} if result.fusion else {}),
        **({"reranking": asdict(result.reranking)} if result.reranking else {}),
        **(
            {"contextualization": asdict(result.contextualization)}
            if result.contextualization
            else {}
        ),
        **(
            {"context_prefixes": [n.to_dict() for n in result.context_prefixes]}
            if result.contextualization
            else {}
        ),
        "answerable": bool(sources),
        "hit": first is not None,
        "rank": first,
        "reciprocal_rank": 1 / first if first is not None else 0.0,
        "relevant_count": len(sources),
        "recalled_count": len(matched),
        "recall_at_k": len(matched) / len(sources) if sources else None,
        "expected": [
            dict(source, retrieved_rank=matched.get(i)) for i, source in enumerate(sources)
        ],
        "empty": not hits,
        "elapsed_ms": elapsed,
        "returned_chars": sum(len(hit.chunk.text) for hit in hits),
        "retrieved": [
            {
                "path": Path(hit.chunk.path).as_posix(),
                "start_line": hit.chunk.start_line,
                "end_line": hit.chunk.end_line,
                "text": hit.chunk.text,
                "score": round(hit.score, 6),
                "chunk_id": hit.chunk.chunk_id,
                "version": hit.chunk.version,
                "retrieval_ranks": [asdict(rank) for rank in hit.retrieval_ranks],
                **({"rerank_score": hit.rerank_score} if hit.rerank_score is not None else {}),
            }
            for hit in hits
        ],
    }


def _matches(source: dict[str, Any], chunk: Chunk) -> bool:
    path = Path(chunk.path).as_posix()
    expected = source["path"]
    if not (path == expected or path.endswith("/" + expected)):
        return False
    return source["evidence"] in chunk.text and (
        "version" not in source
        or (source["version"], source["start_line"], source["end_line"])
        == (chunk.version, chunk.start_line, chunk.end_line)
    )


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """MRR 与宏召回按有答案题平均；微召回按全部金标准片段计数，不混入无答案题。"""
    answerable = [row for row in rows if row["answerable"]]
    no_answer = [row for row in rows if not row["answerable"]]
    relevant = sum(row["relevant_count"] for row in answerable)
    recalled = sum(row["recalled_count"] for row in answerable)
    return {
        "answerable": len(answerable),
        "hits": sum(row["hit"] for row in answerable),
        "hit_at_k": sum(row["hit"] for row in answerable) / len(answerable) if answerable else None,
        "mrr_at_k": sum(row["reciprocal_rank"] for row in answerable) / len(answerable)
        if answerable
        else None,
        "macro_recall_at_k": sum(row["recall_at_k"] for row in answerable) / len(answerable)
        if answerable
        else None,
        "micro_recall_at_k": recalled / relevant if relevant else None,
        "relevant_count": relevant,
        "recalled_count": recalled,
        "no_answer": len(no_answer),
        "no_answer_empty": sum(row["empty"] for row in no_answer),
        "no_answer_false_positive_at_k": sum(not row["empty"] for row in no_answer) / len(no_answer)
        if no_answer
        else None,
        "query_time_ms": round(sum(row["elapsed_ms"] for row in rows), 3),
        "returned_chars": sum(row["returned_chars"] for row in rows),
    }


def _validate_cases(cases: object) -> list[dict[str, Any]]:
    """先验证整套题再查询；旧单证据题标准化为数组，新题拒绝重复或未定位的金标准。"""
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise DocumentError("评估题集必须是 1～1000 项的 JSON 数组。")
    identifiers: set[str] = set()
    validated = []
    for item in cases:
        if (
            not isinstance(item, dict)
            or item.keys()
            not in (
                {"id", "kind", "query", "path", "evidence"},
                {"id", "kind", "query", "relevant"},
            )
            or any(
                not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > maximum
                for key, maximum in (("id", 128), ("kind", 20), ("query", 1000))
            )
            or item["id"] in identifiers
            or item["kind"]
            not in {"exact", "answer", "ambiguous", "paraphrase", "stale", "no_answer"}
        ):
            raise DocumentError("评估题目字段、类别或 ID 无效。")
        identifiers.add(item["id"])
        legacy = "relevant" not in item
        sources = item.get("relevant")
        if legacy:
            sources = (
                []
                if item["path"] is None and item["evidence"] is None
                else [{"path": item["path"], "evidence": item["evidence"]}]
            )
        if (
            not isinstance(sources, list)
            or len(sources) > 20
            or (item["kind"] == "no_answer") != (len(sources) == 0)
        ):
            raise DocumentError("有答案题须有 1～20 个相关片段；无答案题的相关片段必须为空。")
        normalized = [_validate_source(source, legacy=legacy) for source in sources]
        identities = [(s["path"], s.get("start_line"), s.get("end_line")) for s in normalized]
        if len(set(identities)) != len(identities):
            raise DocumentError("同一题的金标准片段位置重复。")
        validated.append(
            {"id": item["id"], "kind": item["kind"], "query": item["query"], "relevant": normalized}
        )
    return validated


def _validate_source(source: object, *, legacy: bool) -> dict[str, Any]:
    """新金标准绑定文件字节版本和实际分块行号；旧题保留路径尾部与原文匹配契约。"""
    fields = (
        {"path", "evidence"}
        if legacy
        else {"path", "evidence", "version", "start_line", "end_line"}
    )
    if (
        not isinstance(source, dict)
        or source.keys() != fields
        or not isinstance(source["path"], str)
        or not source["path"].strip()
        or Path(source["path"]).is_absolute()
        or Path(source["path"]).drive
        or ".." in Path(source["path"]).parts
        or not isinstance(source["evidence"], str)
        or not source["evidence"].strip()
    ):
        raise DocumentError("相关片段须有相对路径和非空原文依据。")
    if not legacy and (
        not isinstance(source["version"], str)
        or re.fullmatch(r"[0-9a-f]{64}", source["version"]) is None
        or type(source["start_line"]) is not int
        or type(source["end_line"]) is not int
        or not 1 <= source["start_line"] <= source["end_line"]
    ):
        raise DocumentError("相关片段须绑定 SHA-256 文件版本和有效的分块行号。")
    return {**source, "path": Path(source["path"]).as_posix()}

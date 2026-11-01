"""校验总结中的原文出处；只证明引用存在且版本有效，不判断结论是否由引用推出。"""

import re
from typing import Any

from adam.documents.index import DocumentIndex
from adam.documents.models import Chunk, DocumentError


def audit_summary(index: DocumentIndex, summary: dict[str, Any]) -> dict[str, Any]:
    """重新读取当前已授权语料，核对每条主张的引用和快照版本。

    summary 包含 source_generation 与 claims；每条 claim 有 text 和 citations。
    citation 使用 chunk_id、path、version、start_line/end_line、start/end 和 quote，
    字符区间相对于分块。缺失、改写、错绑或过时引用不会标为有效；格式非法直接拒绝。
    返回中的 provenance_valid 只说明来源核对成功，semantic_support 始终未检查。
    """
    _validate_summary(summary)
    corpus = index.refresh()
    if summary["source_generation"] != corpus.generation:
        return {
            "status": "stale_source",
            "source_generation": corpus.generation,
            "claimed_generation": summary["source_generation"],
            "semantic_support": "not_checked",
            "claims": [],
            "valid_citations": 0,
        }
    chunks = {chunk.chunk_id: chunk for chunk in corpus.chunks}
    results = []
    cited_ids: set[str] = set()
    valid = 0
    for claim in summary["claims"]:
        index.check_cancel()
        checked = []
        for citation in claim["citations"]:
            error = _citation_error(chunks.get(citation["chunk_id"]), citation)
            checked.append({"chunk_id": citation["chunk_id"], "status": error or "valid"})
            if error is None:
                valid += 1
                cited_ids.add(citation["chunk_id"])
        results.append(
            {
                "text": claim["text"],
                "citations": checked,
                "status": "missing_citation"
                if not checked
                else "provenance_valid"
                if all(c["status"] == "valid" for c in checked)
                else "invalid_citation",
            }
        )
    index.check_cancel()
    return {
        "status": "provenance_valid"
        if results and all(r["status"] == "provenance_valid" for r in results)
        else "invalid_evidence",
        "source_generation": corpus.generation,
        "semantic_support": "not_checked",
        "claims": results,
        "valid_citations": valid,
        "cited_chunks": len(cited_ids),
        "total_chunks": len(chunks),
        "cited_files": len({chunks[identity].path for identity in cited_ids}),
        "total_files": corpus.file_count,
    }


def quote_chunk(chunk: Chunk, *, start: int = 0, end: int | None = None) -> dict[str, Any]:
    """从实际分块生成带精确范围的引用；不接受外部文本替换 quote。"""
    end = len(chunk.text) if end is None else end
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(chunk.text):
        raise DocumentError("原文引用字符区间无效。")
    return {
        "chunk_id": chunk.chunk_id,
        "path": chunk.path,
        "version": chunk.version,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "start": start,
        "end": end,
        "quote": chunk.text[start:end],
    }


def _citation_error(chunk: Chunk | None, citation: dict[str, Any]) -> str | None:
    """只在当前授权语料中查身份，不读取模型提供的路径。"""
    if chunk is None:
        return "missing_chunk"
    if any(
        citation[name] != getattr(chunk, name)
        for name in ("path", "version", "start_line", "end_line")
    ):
        return "source_mismatch"
    start, end = citation["start"], citation["end"]
    if end > len(chunk.text) or chunk.text[start:end] != citation["quote"]:
        return "quote_mismatch"
    return None


def _validate_summary(summary: object) -> None:
    """先限制输入结构和总文本量，再进行文件读取；无引用是可诊断状态，不是成功。"""
    try:
        if not isinstance(summary, dict) or summary.keys() != {"source_generation", "claims"}:
            raise ValueError("summary schema")
        if (
            not isinstance(summary["source_generation"], str)
            or re.fullmatch(r"[0-9a-f]{64}", summary["source_generation"]) is None
        ):
            raise ValueError("source identity")
        claims = summary["claims"]
        if not isinstance(claims, list) or len(claims) > 32:
            raise ValueError("claim count")
        size = 0
        for claim in claims:
            if not isinstance(claim, dict) or claim.keys() != {"text", "citations"}:
                raise ValueError("claim schema")
            if (
                not isinstance(claim["text"], str)
                or not claim["text"].strip()
                or len(claim["text"]) > 4000
            ):
                raise ValueError("claim text")
            citations = claim["citations"]
            if not isinstance(citations, list) or len(citations) > 8:
                raise ValueError("citation count")
            size += len(claim["text"])
            seen = set()
            for citation in citations:
                identity = _validate_citation(citation)
                if identity in seen:
                    raise ValueError("duplicate citation")
                seen.add(identity)
                size += len(citation["quote"]) + len(citation["path"])
                if size > 64000:
                    raise ValueError("summary limit")
        if size > 64000:
            raise ValueError("summary limit")
    except (ValueError, TypeError, KeyError) as error:
        raise DocumentError("知识总结格式或大小无效；未读取来源。") from error


def _validate_citation(citation: object) -> tuple[str, int, int]:
    """限制单条来源和字符区间格式；实际版本、位置和逐字内容由当前语料核对。"""
    if not isinstance(citation, dict) or citation.keys() != {
        "chunk_id",
        "path",
        "version",
        "start_line",
        "end_line",
        "start",
        "end",
        "quote",
    }:
        raise ValueError("citation schema")
    for key, length in (("chunk_id", 16), ("version", 64)):
        if (
            not isinstance(citation[key], str)
            or re.fullmatch(f"[0-9a-f]{{{length}}}", citation[key]) is None
        ):
            raise ValueError("citation identity")
    if not isinstance(citation["path"], str) or not 1 <= len(citation["path"]) <= 4000:
        raise ValueError("citation path")
    if any(type(citation[name]) is not int for name in ("start_line", "end_line", "start", "end")):
        raise ValueError("citation offsets")
    if (
        not 1 <= citation["start_line"] <= citation["end_line"]
        or not 0 <= citation["start"] < citation["end"]
    ):
        raise ValueError("citation range")
    if not isinstance(citation["quote"], str) or not 1 <= len(citation["quote"]) <= 4000:
        raise ValueError("citation quote")
    return citation["chunk_id"], citation["start"], citation["end"]

"""E2.1：固定文档搜索基线，验证来源及检索边界，不模拟检索结果。"""

import hashlib
import json
from pathlib import Path

import pytest

from adam.documents.models import DocumentError
from adam.documents.retrieval import keyword_search
from adam.documents.text import split_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def corpus_chunks():
    """使用人工编写的练习资料；其中的参数不是 Adam 的实际运行配置。"""
    return [
        chunk
        for path in sorted((FIXTURES / "documents").iterdir())
        for chunk in split_document(
            path.name,
            path.read_text(encoding="utf-8"),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    ]


def test_keyword_baseline_finds_literal_answers_with_source():
    cases = json.loads((FIXTURES / "document_questions.json").read_text(encoding="utf-8"))
    chunks = corpus_chunks()
    for case in cases:
        hits = keyword_search(chunks, case["query"], 3)
        if case["kind"] in {"answer", "ambiguous", "stale"}:
            assert any(
                hit.chunk.path == case["path"] and case["evidence"] in hit.chunk.text
                for hit in hits
            ), case["id"]
    assert keyword_search(chunks, "火星基地氧气配给") == []


def test_chunk_locations_sizes_and_revision_identity():
    text = "# 标题\n\n第一段说明。\n第二行依据。\n\n## 另一个标题\n" + "很长的段落" * 60
    chunks = split_document("guide.md", text, "version1", 64)
    assert all(len(chunk.text) <= 64 for chunk in chunks)
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)
    assert chunks[1].start_line == 3 and chunks[1].end_line == 4
    assert chunks[1].title == "标题"
    assert "".join(chunk.text for chunk in chunks[3:]) == "很长的段落" * 60
    assert split_document("guide.md", text, "version1", 64) == chunks
    changed = split_document("guide.md", text, "version2", 64)
    assert not {c.chunk_id for c in changed} & {c.chunk_id for c in chunks}
    assert split_document("empty.md", "\n  \n", "version") == []


@pytest.mark.parametrize("query,limit", [("", 5), ("x" * 1001, 5), ("query", 0), ("q", True)])
def test_query_limits_are_checked_even_with_empty_corpus(query, limit):
    with pytest.raises(DocumentError):
        keyword_search([], query, limit)

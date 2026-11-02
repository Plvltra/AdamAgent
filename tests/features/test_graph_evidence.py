"""C08.1：实际文件/授权索引提取关系候选，逐字证据与同名身份分别验证。"""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from adam.config.settings import DocumentConfig
from adam.documents.graph.evidence import extract_graph
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from tests.features.test_document_index import make_index

DOCUMENTS = {
    "calls.md": "# Calls\n\n[module:checkout|Checkout] calls [service:live|Atlas].\n[module:legacy|Checkout] calls [service:test|Atlas].\n",
    "owners.md": "# Owners\n\n[service:live|Atlas] maintained_by [team:billing|Billing].\n[service:test|Atlas] maintained_by [team:sandbox|Sandbox].\n",
}


def corpus_at(root):
    index = make_index(root)
    for name, text in DOCUMENTS.items():
        (root / "docs" / name).write_text(text, encoding="utf-8")
    return index, index.refresh()


def test_real_corpus_keeps_typed_identity_versions_and_exact_evidence(tmp_path):
    index, corpus = corpus_at(tmp_path)
    graph = extract_graph(corpus)
    assert len(graph.edges) == 4 and graph.generation == corpus.generation
    assert len({e.identity for e in graph.edges}) == 4
    atlas = {e.target.identity for e in graph.edges if e.target.label == "Atlas"}
    assert atlas == {"service:live", "service:test"}
    for edge in graph.edges:
        evidence = edge.evidence
        chunk = next(c for c in corpus.chunks if c.chunk_id == evidence.chunk_id)
        assert evidence.text == chunk.text[evidence.start : evidence.end]
        assert evidence.version == hashlib.sha256(Path(evidence.path).read_bytes()).hexdigest()
        assert (evidence.start_line, evidence.end_line) == (chunk.start_line, chunk.end_line)
        assert edge.extraction == "explicit_marker_v1"
    assert extract_graph(index.refresh()) == graph
    # 重复传入相同片段不会制造第二条同来源关系；不同来源的冲突则保留。
    assert extract_graph(replace(corpus, chunks=corpus.chunks * 2)) == graph


def test_conflicting_candidates_remain_separate_and_do_not_publish_knowledge(tmp_path):
    index, first = corpus_at(tmp_path)
    (tmp_path / "docs" / "disputed.md").write_text(
        "[service:live|Atlas] maintained_by [team:other|Other].", encoding="utf-8"
    )
    graph = extract_graph(index.refresh())
    owners = [e for e in graph.edges if e.subject.identity == "service:live"]
    assert {e.target.identity for e in owners} == {"team:billing", "team:other"}
    assert graph.generation != first.generation
    assert not (tmp_path / "_knowledge").exists()


def test_unmarked_or_incomplete_text_cannot_turn_into_inferred_facts(tmp_path):
    index = make_index(tmp_path)
    (tmp_path / "docs" / "text.md").write_text(
        "Checkout probably calls Atlas.\n[module:checkout|Checkout] may call [service:live|Atlas].\n"
        "[module:checkout|Checkout] calls [service:live|Atlas\n",
        encoding="utf-8",
    )
    assert extract_graph(index.refresh()).edges == ()


def test_limit_failure_does_not_modify_source_or_existing_index(tmp_path):
    index, corpus = corpus_at(tmp_path)
    original = index.path.read_bytes()
    with pytest.raises(DocumentError, match="未返回部分图"):
        extract_graph(corpus, max_edges=3)
    assert index.path.read_bytes() == original
    assert (tmp_path / "docs" / "calls.md").read_text(encoding="utf-8") == DOCUMENTS["calls.md"]


def test_rejected_source_scope_never_enters_graph(tmp_path):
    allowed = make_index(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    config = DocumentConfig(roots=(outside,))

    def reject(path):
        raise DocumentError("injected original authorization boundary")

    denied = DocumentIndex(tmp_path / "denied-index", tmp_path, config, reject)
    with pytest.raises(DocumentError, match="authorization"):
        extract_graph(denied.refresh())
    assert not denied.path.exists() and not allowed.path.exists()

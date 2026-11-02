"""C08.3：真实源变更使图失效，源级候选合并不自动发布长期知识。"""

from dataclasses import replace
from pathlib import Path

import pytest

from adam.documents.graph.retrieval import GraphSearch
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from tests.features.test_document_embeddings import FakeEmbedding
from tests.features.test_graph_evidence import corpus_at


@pytest.mark.parametrize("method", ["keyword", "hybrid"])
def test_merge_original_hybrid_port_by_chunk_source_without_duplicate_or_publication(
    tmp_path, method
):
    index, _ = corpus_at(tmp_path)
    index.config = replace(index.config, method=method)
    service = GraphSearch(DocumentSearch(index, FakeEmbedding()))
    result = service.search("Checkout", "module:checkout", "team:billing", limit=1)
    assert result.text.method == method
    assert (result.text.fusion is not None) == (method == "hybrid")
    assert len(result.graph.paths) == 1 and len(result.graph.paths[0]) == 2
    assert len(result.text.hits) == 1 and len(result.candidates) == 2
    assert any(c.methods == ("text", "graph") for c in result.candidates)
    assert any(c.methods == ("graph",) for c in result.candidates)
    assert len({c.chunk.chunk_id for c in result.candidates}) == 2
    assert result.text.generation == result.graph.generation
    assert not (tmp_path / "_knowledge").exists()
    assert all(
        c.chunk.text in Path(c.chunk.path).read_text(encoding="utf-8") for c in result.candidates
    )


def test_edit_and_delete_source_cannot_keep_previous_edges(tmp_path):
    index, _ = corpus_at(tmp_path)
    service = GraphSearch(DocumentSearch(index))
    first = service.search("Checkout", "module:checkout", "team:billing")
    path = tmp_path / "docs/owners.md"
    path.write_text("[service:live|Atlas] maintained_by [team:other|Other].", encoding="utf-8")
    stale = service.search("Checkout", "module:checkout", "team:billing")
    assert first.graph.paths and not stale.graph.paths
    current = service.search("Checkout", "module:checkout", "team:other")
    assert current.graph.paths and current.graph.generation != first.graph.generation
    path.unlink()
    assert not service.search("Checkout", "module:checkout", "team:other").graph.paths


def test_source_failure_and_in_between_change_are_not_silent_fallback(tmp_path, monkeypatch):
    index, _ = corpus_at(tmp_path)
    service = GraphSearch(DocumentSearch(index))
    original = index.refresh
    calls = 0

    def changed():
        nonlocal calls
        calls += 1
        if calls == 2:
            (tmp_path / "docs/new.md").write_text("new source", encoding="utf-8")
        return original()

    monkeypatch.setattr(index, "refresh", changed)
    with pytest.raises(DocumentError, match="来源已变化"):
        service.search("Checkout", "module:checkout")
    (tmp_path / "docs/new.md").write_bytes(b"\xff")
    with pytest.raises(DocumentError, match="UTF-8"):
        service.search("Checkout", "module:checkout")


def test_revoked_authorization_and_cancellation_do_not_reuse_graph(tmp_path):
    from adam.contracts.cancellation import CancellationToken
    from adam.contracts.errors import RequestInterrupted

    index, _ = corpus_at(tmp_path)
    service = GraphSearch(DocumentSearch(index))
    assert service.search("Checkout", "module:checkout").graph.paths
    previous = index.path.read_bytes()
    token = CancellationToken()
    index.check_cancel = token.check
    token.cancel()
    with pytest.raises(RequestInterrupted):
        service.search("Checkout", "module:checkout")
    assert index.path.read_bytes() == previous
    index.check_cancel = lambda: None

    def denied(path):
        # 已有索引后撤销原文件授权，不能借旧图继续交付证据。
        raise DocumentError("injected revoked source authorization")

    index.source.authorize = denied
    with pytest.raises(DocumentError, match="revoked"):
        service.search("Checkout", "module:checkout")
    assert index.path.read_bytes() == previous

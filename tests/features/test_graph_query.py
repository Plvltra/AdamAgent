"""C08.2：真实语料两跳证据、同名选择、无答案与资源边界。"""

import pytest

from adam.documents.graph.evidence import extract_graph
from adam.documents.graph.query import query_paths
from adam.documents.models import DocumentError
from tests.features.test_graph_evidence import corpus_at


def test_two_hop_path_returns_both_original_source_versions(tmp_path):
    _, corpus = corpus_at(tmp_path)
    graph = extract_graph(corpus)
    result = query_paths(graph, "module:checkout", "team:billing")
    assert len(result.paths) == 1 and len(result.paths[0]) == 2
    assert [e.relation for e in result.paths[0]] == ["calls", "maintained_by"]
    assert [e.target.identity for e in result.paths[0]] == ["service:live", "team:billing"]
    assert len({e.evidence.path for e in result.paths[0]}) == 2
    assert result.generation == corpus.generation
    assert all(e in graph.edges for e in result.paths[0])
    assert query_paths(graph, "module:checkout", "team:billing", max_hops=1).paths == ()
    assert query_paths(graph, "module:checkout", "team:missing").paths == ()
    assert query_paths(graph, "module:legacy", "team:billing").paths == ()
    assert query_paths(graph, "team:billing", "module:checkout").paths == ()
    with pytest.raises(ValueError, match="IDs"):
        query_paths(graph, "Checkout")


@pytest.mark.parametrize("limits", [{"max_edges": 1}, {"max_nodes": 2}])
def test_traversal_limit_cannot_look_like_no_answer(tmp_path, limits):
    _, corpus = corpus_at(tmp_path)
    with pytest.raises(DocumentError, match="不完整答案"):
        query_paths(extract_graph(corpus), "module:checkout", **limits)


def test_cycle_never_returns_an_inferred_or_infinite_path(tmp_path):
    index, _ = corpus_at(tmp_path)
    (tmp_path / "docs/cycle.md").write_text(
        "[team:billing|Billing] calls [module:checkout|Checkout].", encoding="utf-8"
    )
    graph = extract_graph(index.refresh())
    result = query_paths(graph, "module:checkout", max_hops=4)
    assert [len(p) for p in result.paths] == [1, 2]
    assert result.traversed_edges == 3 and result.visited_nodes == 3

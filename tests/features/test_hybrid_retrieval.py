"""B09.1：真实文档索引、BM25 和向量缓存融合；受控向量不证明语义质量。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import DocumentConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted
from adam.document_search import main
from adam.documents.fusion import reciprocal_rank_fusion
from adam.documents.models import DocumentError, SearchHit
from adam.documents.search import DocumentSearch
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import encode_result, pack_results
from tests.features.test_document_embeddings import FakeEmbedding
from tests.features.test_document_index import make_index
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def corpus(root, *, candidates=20):
    index = make_index(root)
    index.config = replace(index.config, method="hybrid", hybrid_candidates=candidates)
    docs = root / "docs"
    (docs / "a.txt").write_text("ZX_17 ZX_17 ZX_17", encoding="utf-8")
    (docs / "b.txt").write_text("ZX_17 网络连接故障详细重试约定请核对", encoding="utf-8")
    (docs / "c.txt").write_text("网络纯语义资料", encoding="utf-8")
    return index


def test_union_deduplicates_and_uses_candidate_pool_before_final_top_k(tmp_path):
    index = corpus(tmp_path)
    search = DocumentSearch(index, FakeEmbedding())
    result = search.search("ZX_17", 1)
    assert Path(result.hits[0].chunk.path).name == "b.txt"
    assert [(r.method, r.rank) for r in result.hits[0].retrieval_ranks] == [
        ("keyword", 2),
        ("semantic", 1),
    ]
    assert result.hits[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert result.fusion.keyword_candidates == result.fusion.semantic_candidates == 2
    all_hits = search.search("ZX_17", 20)
    assert len(all_hits.hits) == len({h.chunk.chunk_id for h in all_hits.hits}) == 3
    assert all_hits.generation == result.generation
    index.config = replace(index.config, hybrid_candidates=1)
    narrow = search.search("ZX_17", 1)
    assert Path(narrow.hits[0].chunk.path).name == "a.txt"


def test_keyword_and_semantic_modes_remain_independent_and_comparable(tmp_path):
    index = corpus(tmp_path)
    backend = FakeEmbedding()
    search = DocumentSearch(index, backend)
    index.config = replace(index.config, method="keyword")
    keyword = search.search("ZX_17", 3)
    assert backend.inputs == [] and keyword.fusion is None
    assert [Path(h.chunk.path).name for h in keyword.hits] == ["a.txt", "b.txt"]
    index.config = replace(index.config, method="semantic")
    semantic = search.search("ZX_17", 3)
    assert semantic.fusion is None and semantic.generation == keyword.generation
    assert [Path(h.chunk.path).name for h in semantic.hits] == ["b.txt", "c.txt"]
    assert all(not h.retrieval_ranks for h in (*keyword.hits, *semantic.hits))


def test_same_route_duplicate_cannot_boost_and_mismatched_source_is_rejected(tmp_path):
    chunks = corpus(tmp_path).refresh().chunks
    a, b = (SearchHit(chunk, 1.0) for chunk in chunks[:2])
    result = reciprocal_rank_fusion([a, a, b], [b], 3)
    assert result[0].chunk == b.chunk
    assert result[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert result[1].score == pytest.approx(1 / 61)
    with pytest.raises(DocumentError, match="不一致"):
        reciprocal_rank_fusion([a], [replace(a, chunk=replace(a.chunk, text="forged"))], 3)
    with pytest.raises(DocumentError, match="无效分数"):
        reciprocal_rank_fusion([replace(a, score=float("nan"))], [], 3)


def test_source_versions_update_and_removed_chunks_do_not_survive_fusion(tmp_path):
    index = corpus(tmp_path)
    backend = FakeEmbedding()
    search = DocumentSearch(index, backend)
    old = search.search("ZX_17", 3)
    source = tmp_path / "docs" / "b.txt"
    old_hit = next(h for h in old.hits if h.chunk.path == str(source))
    source.write_text("ZX_17 网络现在采用新约定", encoding="utf-8")
    updated = search.search("ZX_17", 3)
    current = next(h for h in updated.hits if h.chunk.path == str(source))
    assert (
        current.chunk.version != old_hit.chunk.version
        and current.chunk.chunk_id != old_hit.chunk.chunk_id
    )
    assert {r.method for r in current.retrieval_ranks} == {"keyword", "semantic"}
    source.unlink()
    assert all(h.chunk.path != str(source) for h in search.search("ZX_17", 3).hits)


@pytest.mark.parametrize("fault", ["model", "cache", "dimension", "cancel"])
def test_failed_vector_branch_does_not_masquerade_as_complete_hybrid(tmp_path, fault):
    index = corpus(tmp_path)
    backend = FakeEmbedding()
    search = DocumentSearch(index, backend)
    search.search("ZX_17", 3)
    if fault == "model":

        def unavailable(_):
            raise DocumentError("local embedding unavailable")

        backend.query = unavailable
    elif fault == "cache":
        search.vectors.path.write_bytes(b"{broken")
    elif fault == "dimension":
        backend.query = lambda _: [1.0, 0.0, 0.0]
    else:
        cancel = CancellationToken()

        def stop(_):
            cancel.cancel()
            cancel.check()

        backend.query = stop
    with pytest.raises(RequestInterrupted if fault == "cancel" else DocumentError):
        search.search("ZX_17", 3)


def test_empty_single_branch_is_valid_but_does_not_claim_answer_confidence(tmp_path):
    index = corpus(tmp_path)
    result = DocumentSearch(index, FakeEmbedding()).search("完全不同的字面问题", 3)
    assert result.fusion.keyword_candidates == 0 and result.fusion.semantic_candidates == 2
    assert all(
        len(h.retrieval_ranks) == 1 and h.retrieval_ranks[0].method == "semantic"
        for h in result.hits
    )
    for path in (tmp_path / "docs").iterdir():
        path.unlink()
    empty = DocumentSearch(index, FakeEmbedding()).search("问题", 3)
    assert (
        empty.hits == ()
        and empty.fusion.keyword_candidates == empty.fusion.semantic_candidates == 0
    )


def test_whole_tool_result_limit_keeps_original_text_and_fusion_evidence(tmp_path):
    result = DocumentSearch(corpus(tmp_path), FakeEmbedding()).search("ZX_17", 3)
    full = pack_results(result, 8000)
    assert full["score_kind"] == "reciprocal_rank_fusion" and full["method"] == "hybrid"
    for actual, hit in zip(full["results"], result.hits, strict=True):
        assert actual["text"] == hit.chunk.text and actual["version"] == hit.chunk.version
        assert len(actual["retrieval_ranks"]) == len(hit.retrieval_ranks)
    bounded = pack_results(result, 512)
    assert bounded["status"] == "output_limit" and bounded["truncated"]
    assert len(encode_result(bounded)) <= 512


def test_real_tool_logs_hybrid_sources_and_replay_does_not_load_embeddings(tmp_path, monkeypatch):
    corpus(tmp_path)
    config = replace(
        config_at(tmp_path), documents=DocumentConfig(roots=(tmp_path / "docs",), method="hybrid")
    )
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())

    def respond(body, number):
        if number == 1:
            response = tool_response([call("search", "search_documents", query="ZX_17", limit=1)])
        else:
            result = outputs(body)[-1]
            assert result["method"] == "hybrid" and len(result["results"]) == 1
            assert len(result["results"][0]["retrieval_ranks"]) == 2
            response = answer("document b supplied evidence")
        return httpx.Response(200, json=response)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, _):
        assert recorder.run(lambda: app.send("task")) == "document b supplied evidence"
        events = store.load_events(app.session.session_id)
        assert any(
            "reciprocal_rank_fusion" in json.dumps(e)
            for e in events
            if e["type"] == "tool_finished"
        )
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        DocumentSearch, "search", lambda *_: pytest.fail("offline replay queried current documents")
    )
    assert replay_turn(source, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize(
    "setting",
    ["hybrid_candidates=0", "hybrid_candidates=21", "rrf_constant=true", "rrf_constant=0"],
)
def test_invalid_fusion_config_is_rejected(tmp_path, setting):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(f'[documents]\nmethod="hybrid"\n{setting}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_hybrid_cli_and_toml_expose_fusion_without_calling_chat_api(tmp_path, monkeypatch, capsys):
    corpus(tmp_path)
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "cli-cache"),
                "--hybrid",
                "--rrf-constant",
                "40",
                "--limit",
                "1",
                "search",
                "ZX_17",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert (
        result["fusion"]["rrf_constant"] == 40 and result["score_kind"] == "reciprocal_rank_fusion"
    )
    assert Path(result["results"][0]["path"]).name == "b.txt"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        '[documents]\nmethod="hybrid"\nhybrid_candidates=10\nrrf_constant=40', encoding="utf-8"
    )
    config = load_config(path).documents
    assert (
        config.method == "hybrid" and config.hybrid_candidates == 10 and config.rrf_constant == 40
    )

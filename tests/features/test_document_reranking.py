"""B09.2：可选精排只改候选次序；实际索引与缓存、受控模型和故障边界。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.settings import DocumentConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted
from adam.contracts.reranking import RerankError
from adam.document_search import main
from adam.documents.models import DocumentError
from adam.documents.reranking import FastEmbedReranker
from adam.documents.search import DocumentSearch
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import pack_results
from tests.features.test_document_embeddings import FakeEmbedding
from tests.features.test_hybrid_retrieval import corpus
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


class ControlledReranker:
    """仅代替 cross-encoder 分数；偏好第三个候选用于证明精排发生在最终截断之前。"""

    identity = "controlled-reranker-v1"

    def __init__(self):
        self.calls = []

    def score(self, query, passages):
        self.calls.append((query, passages))
        return [9.0 if "纯语义" in text else -1.0 for text in passages]


def enabled_index(root):
    index = corpus(root)
    index.config = replace(index.config, rerank=True)
    return index


def test_reranker_rescores_candidate_pool_and_keeps_original_fusion_evidence(tmp_path):
    index = enabled_index(tmp_path)
    ranker = ControlledReranker()
    search = DocumentSearch(index, FakeEmbedding(), reranker=ranker)
    result = search.search("ZX_17", 1)
    assert len(ranker.calls) == 3 and len(result.hits) == 1
    hit = result.hits[0]
    assert Path(hit.chunk.path).name == "c.txt" and hit.rerank_score == 9
    assert hit.score == pytest.approx(1 / 62) and hit.retrieval_ranks[0].method == "semantic"
    assert result.reranking.status == "applied" and result.reranking.candidates == 3
    output = pack_results(result, 8000)
    assert output["order"] == "reranker" and output["score_kind"] == "reciprocal_rank_fusion"
    assert output["results"][0]["text"] == "网络纯语义资料"
    assert output["results"][0]["rerank_score"] == 9


@pytest.mark.parametrize("fault", ["raise", "count", "nan", "bool", "text", "identity"])
def test_any_partial_failure_returns_unchanged_fusion_without_partial_scores(tmp_path, fault):
    index = enabled_index(tmp_path)
    plain = replace(index.config, rerank=False)
    ranker = ControlledReranker()
    calls = []

    def bad(query, passages):
        calls.append(passages)
        if len(calls) == 1:
            return [100.0]  # 后一候选失败后，前一候选的分数也必须丢弃。
        if fault == "raise":
            raise RerankError("sensitive provider details must not leak")
        return {"count": [], "nan": [float("nan")], "bool": [True], "text": ["9"]}[fault]

    if fault == "identity":
        ranker.identity = ""
    else:
        ranker.score = bad
    result = DocumentSearch(index, FakeEmbedding(), reranker=ranker).search("ZX_17", 3)
    index.config = plain
    original = DocumentSearch(index, FakeEmbedding()).search("ZX_17", 3)
    assert result.hits == original.hits and result.fusion == original.fusion
    assert result.reranking.status == "fallback" and result.reranking.failure
    output = pack_results(result, 8000)
    assert output["order"] == "fusion" and "sensitive" not in json.dumps(output)
    assert all("rerank_score" not in hit for hit in output["results"])


def test_prefix_bounds_are_reported_but_original_citation_remains_intact(tmp_path):
    index = enabled_index(tmp_path)
    index.config = replace(index.config, rerank_query_chars=5, rerank_passage_chars=4)
    ranker = ControlledReranker()
    result = DocumentSearch(index, FakeEmbedding(), reranker=ranker).search("ZX_17 long query", 3)
    assert all(len(q) <= 5 and all(len(t) <= 4 for t in docs) for q, docs in ranker.calls)
    assert result.reranking.query_truncated and result.reranking.truncated_passages == 3
    assert result.reranking.input_policy == "prefix_then_model_tokenizer"
    assert all(len(hit.chunk.text) > 4 for hit in result.hits)
    with pytest.raises(DocumentError, match="输出限额"):
        pack_results(result, 512)


def test_cancellation_during_ranking_is_not_converted_to_fallback(tmp_path):
    index = enabled_index(tmp_path)
    cancel = CancellationToken()
    index.check_cancel = cancel.check
    ranker = ControlledReranker()

    def stop(query, passages):
        cancel.cancel()
        return [1.0]

    ranker.score = stop
    with pytest.raises(RequestInterrupted):
        DocumentSearch(index, FakeEmbedding(), reranker=ranker).search("ZX_17", 3)


def test_disabled_or_empty_ranking_never_touches_optional_backend(tmp_path):
    index = corpus(tmp_path)
    ranker = ControlledReranker()
    ranker.score = lambda *_: pytest.fail("disabled or empty ranker should not execute")
    search = DocumentSearch(index, FakeEmbedding(), reranker=ranker)
    assert search.search("ZX_17", 3).reranking is None
    for path in (tmp_path / "docs").iterdir():
        path.unlink()
    index.config = replace(index.config, rerank=True)
    result = search.search("nothing", 3)
    assert not result.hits and result.reranking.status == "empty"


def test_local_loader_cannot_download_in_normal_query_and_preparation_is_explicit(
    tmp_path, monkeypatch
):
    options = []

    class Model:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def rerank(self, query, documents, batch_size=1):
            return iter([1.0 for _ in documents])

    class Module:
        TextCrossEncoder = Model

    monkeypatch.setattr("adam.documents.reranking.importlib.metadata.version", lambda _: "test")
    monkeypatch.setattr("adam.documents.reranking.importlib.import_module", lambda _: Module)
    backend = FastEmbedReranker(tmp_path, "test-model")
    assert backend.score("q", ["d"]) == [1.0]
    backend.prepare()
    assert len(options) == 1 and options[0]["local_files_only"] is True
    assert options[0]["providers"] == ["CPUExecutionProvider"] and options[0]["threads"] == 1
    assert (
        main(["--cache-dir", str(tmp_path), "--reranker-model", "test-model", "prepare-reranker"])
        == 0
    )
    assert options[-1]["local_files_only"] is False


def test_missing_dependency_is_reported_as_fallback_with_real_fusion_sources(tmp_path, monkeypatch):
    import importlib.metadata

    def missing(_):
        raise importlib.metadata.PackageNotFoundError("fastembed")

    monkeypatch.setattr("adam.documents.reranking.importlib.metadata.version", missing)
    result = DocumentSearch(enabled_index(tmp_path), FakeEmbedding()).search("ZX_17", 3)
    assert (
        result.reranking.status == "fallback" and result.reranking.failure == "missing_dependency"
    )
    assert Path(result.hits[0].chunk.path).name == "b.txt"


def test_tool_log_and_replay_preserve_applied_ranking_without_rerunning_model(
    tmp_path, monkeypatch
):
    corpus(tmp_path)
    config = replace(
        config_at(tmp_path),
        documents=DocumentConfig(roots=(tmp_path / "docs",), method="hybrid", rerank=True),
    )
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())
    monkeypatch.setattr("adam.documents.search.FastEmbedReranker", lambda *_: ControlledReranker())

    def respond(body, number):
        if number == 1:
            response = tool_response([call("search", "search_documents", query="ZX_17", limit=1)])
        else:
            result = outputs(body)[-1]
            assert result["reranking"]["status"] == "applied" and result["order"] == "reranker"
            assert Path(result["results"][0]["path"]).name == "c.txt"
            response = answer("reranked evidence")
        return httpx.Response(200, json=response)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, _):
        assert recorder.run(lambda: app.send("task")) == "reranked evidence"
        assert any(
            "rerank_score" in json.dumps(e)
            for e in store.load_events(app.session.session_id)
            if e["type"] == "tool_finished"
        )
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(DocumentSearch, "search", lambda *_: pytest.fail("replay reran retrieval"))
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "keyword"},
        {"rerank_candidates": 0},
        {"rerank_query_chars": True},
        {"rerank_passage_chars": 4001},
    ],
)
def test_reranker_config_refuses_unbounded_or_wrong_mode(changes):
    with pytest.raises(ConfigError):
        DocumentConfig(**{"method": "hybrid", "rerank": True, **changes})


def test_cli_evaluation_distinguishes_requested_ranking_from_actual_fallback(
    tmp_path, monkeypatch, capsys
):
    corpus(tmp_path)
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())

    class Unavailable(ControlledReranker):
        def score(self, query, passages):
            raise RerankError("model_unavailable")

    monkeypatch.setattr("adam.documents.search.FastEmbedReranker", lambda *_: Unavailable())
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps(
            [
                {
                    "id": "exact",
                    "kind": "answer",
                    "query": "ZX_17",
                    "path": "b.txt",
                    "evidence": "网络连接",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "cli-cache"),
                "--hybrid",
                "--rerank",
                "evaluate",
                str(questions),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["metadata"]["rerank"] is True and "hybrid_reranked" in result
    row = result["hybrid_reranked"]["rows"][0]
    assert (
        row["reranking"]["status"] == "fallback"
        and row["reranking"]["failure"] == "model_unavailable"
    )
    assert row["fusion"] and row["generation"] and row["retrieved"][0]["version"]

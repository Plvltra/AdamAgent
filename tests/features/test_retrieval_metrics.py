"""B09.3：真实分块与版本金标准；受控排名验证指标，不用假分数证明模型质量。"""

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.document_search import main
from adam.documents.evaluation import evaluate
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError, SearchHit
from adam.documents.search import DocumentSearch, SearchResult
from adam.tool_calls.policy import PathPolicy
from tests.features.test_document_embeddings import FakeEmbedding
from tests.features.test_document_reranking import ControlledReranker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture_index(root):
    shutil.copytree(FIXTURES / "documents", root / "documents")
    policy = PathPolicy(root, PermissionConfig())
    return DocumentIndex(
        root / "cache", root, DocumentConfig(roots=(Path("documents"),)), policy.resolve
    )


def source(chunk):
    return {
        "path": "documents/" + Path(chunk.path).name,
        "version": chunk.version,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "evidence": chunk.text,
    }


def case(identifier, chunks=(), *, kind="answer"):
    return {
        "id": identifier,
        "kind": kind,
        "query": identifier,
        "relevant": [source(c) for c in chunks],
    }


def test_frozen_questions_bind_unique_real_chunks_and_do_not_rewrite_gold(tmp_path):
    path = FIXTURES / "document_multi_questions.json"
    original = path.read_bytes()
    cases = json.loads(original)
    corpus = fixture_index(tmp_path).refresh()
    assert len(cases) == 12
    assert {c["kind"] for c in cases} == {"exact", "paraphrase", "answer", "no_answer"}
    assert sum(len(c["relevant"]) > 1 for c in cases) == 3
    for question in cases:
        for expected in question["relevant"]:
            matches = [
                c
                for c in corpus.chunks
                if c.path.replace("\\", "/").endswith("/" + expected["path"])
                and (c.version, c.start_line, c.end_line)
                == (expected["version"], expected["start_line"], expected["end_line"])
                and expected["evidence"] in c.text
            ]
            assert len(matches) == 1, question["id"]
    assert path.read_bytes() == original


def test_partial_recall_rank_and_no_answer_have_separate_denominators(tmp_path):
    corpus = fixture_index(tmp_path).refresh()
    a, b, c, d, e = corpus.chunks[:5]
    questions = [
        case("partial", [a, b, c]),
        case("full", [d]),
        case("miss", [e]),
        case("empty", kind="no_answer"),
        case("false_positive", kind="no_answer"),
    ]
    ranked = {"partial": [e, a], "full": [d], "miss": [], "empty": [], "false_positive": [e]}

    def controlled(query, limit):
        return SearchResult(corpus.generation, tuple(SearchHit(c, 1.0) for c in ranked[query]), 6)

    report = evaluate(controlled, questions, 2)
    assert report["answerable"] == 3 and report["hits"] == 2
    assert report["hit_at_k"] == pytest.approx(2 / 3)
    assert report["mrr_at_k"] == pytest.approx((0.5 + 1 + 0) / 3)
    assert report["macro_recall_at_k"] == pytest.approx((1 / 3 + 1 + 0) / 3)
    assert report["micro_recall_at_k"] == 2 / 5
    assert report["no_answer_empty"] == 1 and report["no_answer_false_positive_at_k"] == 0.5
    assert report["rows"][0]["rank"] == 2 and report["rows"][0]["recalled_count"] == 1
    assert report["rows"][0]["expected"][0]["retrieved_rank"] == 2
    assert report["by_kind"]["no_answer"]["mrr_at_k"] is None
    assert report["by_kind"]["answer"]["no_answer_false_positive_at_k"] is None
    assert len(report["cases_sha256"]) == 64


def test_changed_source_keeps_old_gold_and_cannot_count_as_a_hit(tmp_path):
    index = fixture_index(tmp_path)
    chunk = next(c for c in index.refresh().chunks if "权限错误不重试" in c.text)
    question = case("权限错误不重试", [chunk])
    old_gold = copy.deepcopy(question)
    path = Path(chunk.path)
    path.write_text(path.read_text(encoding="utf-8") + "\n后加的资料。\n", encoding="utf-8")
    report = evaluate(DocumentSearch(index).search, [question])
    assert not report["rows"][0]["empty"] and report["hits"] == 0
    assert question == old_gold
    assert any(hit["version"] != chunk.version for hit in report["rows"][0]["retrieved"])


def test_source_changes_between_questions_reject_whole_report(tmp_path):
    index = fixture_index(tmp_path)
    engine = DocumentSearch(index)
    chunks = index.refresh().chunks
    calls = []

    def change_after_query(query, limit):
        result = engine.search(query, limit)
        calls.append(query)
        Path(chunks[0].path).write_text("改变资料版本", encoding="utf-8")
        return result

    with pytest.raises(DocumentError, match="语料发生变化"):
        evaluate(change_after_query, [case("first", [chunks[0]]), case("second", [chunks[1]])])
    assert len(calls) == 2


@pytest.mark.parametrize("fault", ["duplicate", "bad_version", "bad_line", "escape", "empty", "id"])
def test_invalid_gold_is_rejected_before_even_first_valid_question(tmp_path, fault):
    chunk = fixture_index(tmp_path).refresh().chunks[0]
    questions = [case("valid", [chunk]), case("invalid", [chunk])]
    bad = questions[1]
    if fault == "duplicate":
        bad["relevant"] *= 2
    elif fault == "bad_version":
        bad["relevant"][0]["version"] = "unknown"
    elif fault == "bad_line":
        bad["relevant"][0]["start_line"] = True
    elif fault == "escape":
        bad["relevant"][0]["path"] = "../private.md"
    elif fault == "empty":
        bad["relevant"] = []
    else:
        bad["id"] = "valid"
    with pytest.raises(DocumentError):
        evaluate(lambda *_: pytest.fail("invalid gold must prevent all queries"), questions)


@pytest.mark.parametrize("fault", ["duplicate", "over_k"])
def test_invalid_result_cannot_inflate_top_k_metrics(tmp_path, fault):
    chunks = fixture_index(tmp_path).refresh().chunks
    selected = [chunks[0], chunks[0]] if fault == "duplicate" else list(chunks[:3])
    result = SearchResult("fixed", tuple(SearchHit(c, 1.0) for c in selected), 6)
    with pytest.raises(DocumentError, match="重复片段"):
        evaluate(lambda *_: result, [case("question", [chunks[0]])], 2)


def test_four_route_cli_keeps_plain_fusion_and_actual_rerank_status(tmp_path, monkeypatch):
    fixture_index(tmp_path)
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())
    monkeypatch.setattr("adam.documents.search.FastEmbedReranker", lambda *_: ControlledReranker())
    output = tmp_path / "comparison.json"
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "--root",
                "documents",
                "--cache-dir",
                str(tmp_path / "eval"),
                "--hybrid",
                "--rerank",
                "--output",
                str(output),
                "evaluate",
                str(FIXTURES / "document_multi_questions.json"),
            ]
        )
        == 0
    )
    reports = json.loads(output.read_text(encoding="utf-8"))
    names = {"keyword", "semantic", "hybrid", "hybrid_reranked"}
    assert reports.keys() == names | {"metadata"}
    assert len({reports[name]["source_generation"] for name in names}) == 1
    assert len({reports[name]["cases_sha256"] for name in names}) == 1
    assert reports["hybrid"]["rows"][0]["fusion"]
    assert "reranking" not in reports["hybrid"]["rows"][0]
    assert reports["hybrid_reranked"]["rows"][0]["reranking"]["status"] == "applied"


def test_change_between_routes_preserves_preexisting_output(tmp_path, monkeypatch):
    index = fixture_index(tmp_path)
    chunk = index.refresh().chunks[0]
    questions = tmp_path / "one.json"
    questions.write_text(json.dumps([case("query", [chunk])]), encoding="utf-8")
    output = tmp_path / "result.json"
    output.write_bytes(b"previous report")
    original = DocumentSearch.search

    def change_after_keyword(engine, query, limit):
        result = original(engine, query, limit)
        if engine.index.config.method == "keyword":
            Path(chunk.path).write_text("新的资料", encoding="utf-8")
        return result

    monkeypatch.setattr(DocumentSearch, "search", change_after_keyword)
    monkeypatch.setattr("adam.documents.search.FastEmbedBackend", lambda *_: FakeEmbedding())
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "--root",
                "documents",
                "--cache-dir",
                str(tmp_path / "eval"),
                "--hybrid",
                "--output",
                str(output),
                "evaluate",
                str(questions),
            ]
        )
        == 1
    )
    assert output.read_bytes() == b"previous report"


def test_all_no_answer_or_invalid_k_has_no_fake_perfect_score():
    result = SearchResult("fixed", (), 0)
    report = evaluate(lambda *_: result, [case("none", kind="no_answer")])
    assert report["hit_at_k"] is None and report["mrr_at_k"] is None
    assert report["macro_recall_at_k"] is None and report["micro_recall_at_k"] is None
    assert report["no_answer_false_positive_at_k"] == 0
    with pytest.raises(DocumentError, match="k"):
        evaluate(lambda *_: pytest.fail("bad k must prevent query"), [], True)


def test_archived_real_run_keeps_all_questions_source_versions_and_failure_cases():
    root = Path(__file__).resolve().parents[2]
    folder = root / "docs/evaluations"
    raw = (folder / "b09-retrieval-real.json").read_bytes()
    manifest = json.loads((folder / "b09-retrieval-manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == manifest["report_sha256"]
    assert all(
        hashlib.sha256((root / p).read_bytes()).hexdigest() == v
        for p, v in manifest["sources"].items()
    )
    gold = FIXTURES / "document_multi_questions.json"
    assert hashlib.sha256(gold.read_bytes()).hexdigest() == manifest["questions"]["sha256"]
    cases = json.loads(gold.read_text(encoding="utf-8"))
    report = json.loads(raw)
    for method in manifest["execution_order"]:
        result = report[method]
        assert [r["id"] for r in result["rows"]] == [c["id"] for c in cases]
        assert result["answerable"] == 10 and result["relevant_count"] == 13
        for row, question in zip(result["rows"], cases, strict=True):
            assert [
                {k: v for k, v in e.items() if k != "retrieved_rank"} for e in row["expected"]
            ] == question["relevant"]
            for hit in row["retrieved"]:
                path = FIXTURES / "documents" / Path(hit["path"]).name
                assert hashlib.sha256(path.read_bytes()).hexdigest() == hit["version"]
                lines = path.read_text(encoding="utf-8").splitlines()
                assert (
                    "\n".join(lines[hit["start_line"] - 1 : hit["end_line"]]).strip() == hit["text"]
                )
    assert report["keyword"]["by_kind"]["paraphrase"]["hits"] == 0
    assert report["semantic"]["recalled_count"] == 12
    assert report["hybrid"]["mrr_at_k"] < report["hybrid_reranked"]["mrr_at_k"]
    assert {r["reranking"]["status"] for r in report["hybrid_reranked"]["rows"]} == {
        "applied",
        "empty",
    }

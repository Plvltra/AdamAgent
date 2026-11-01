"""B11.3：固定资料的全库概览/单点查询对照及缺失、过时、错绑引用验收。"""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.settings import DocumentConfig
from adam.contracts.errors import ToolError
from adam.documents.evidence_audit import audit_summary, quote_chunk
from adam.documents.models import DocumentError
from scripts.knowledge_eval import DOCUMENTS, main, run_suite
from tests.features.test_knowledge_catalog import catalog_index
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app


def cited_summary(index):
    corpus = index.refresh()
    source = next(c for c in corpus.chunks if "权限" in c.text)
    return {
        "source_generation": corpus.generation,
        "claims": [{"text": source.text, "citations": [quote_chunk(source)]}],
    }


def test_exact_current_quote_is_valid_but_does_not_prove_claim_meaning(tmp_path):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)
    correct = audit_summary(index, summary)
    assert correct["status"] == "provenance_valid" and correct["valid_citations"] == 1
    assert correct["cited_chunks"] == correct["cited_files"] == 1
    assert correct["total_files"] == 3 and correct["total_chunks"] > 1
    summary["claims"][0]["text"] = "所有权限错误都应该无限重试。"
    misleading = audit_summary(index, summary)
    assert misleading["status"] == "provenance_valid"
    assert misleading["semantic_support"] == "not_checked"
    assert "answer_correct" not in misleading


@pytest.mark.parametrize("kind", ["no_claims", "uncited"])
def test_empty_and_uncited_summaries_cannot_pass_source_check(tmp_path, kind):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)
    if kind == "no_claims":
        summary["claims"] = []
    else:
        summary["claims"][0]["citations"] = []
    result = audit_summary(index, summary)
    assert result["status"] == "invalid_evidence" and result["valid_citations"] == 0
    if kind == "uncited":
        assert result["claims"][0]["status"] == "missing_citation"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("chunk_id", "0" * 16, "missing_chunk"),
        ("path", "C:/ungranted-secret.md", "source_mismatch"),
        ("version", "0" * 64, "source_mismatch"),
        ("end_line", 999, "source_mismatch"),
        ("quote", "权限错误无限重试", "quote_mismatch"),
        ("end", 99999, "quote_mismatch"),
    ],
)
def test_missing_stale_and_altered_citation_is_reported_without_reading_its_path(
    tmp_path, field, value, reason
):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)
    summary["claims"][0]["citations"][0][field] = value
    authorized = []
    original = index.source.authorize

    def record(path):
        authorized.append(path)
        return original(path)

    index.source.authorize = record
    result = audit_summary(index, summary)
    assert result["status"] == "invalid_evidence"
    assert result["claims"][0]["citations"][0]["status"] == reason
    assert result["valid_citations"] == 0
    assert not any("ungranted-secret" in path for path in authorized)


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_actual_source_change_rejects_whole_old_summary_without_rewriting_it(tmp_path, change):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)
    before = copy.deepcopy(summary)
    source = tmp_path / "docs/a.md"
    if change == "edit":
        source.write_text("# Network\n\n现在约定改变了。", encoding="utf-8")
    else:
        source.unlink()
    result = audit_summary(index, summary)
    assert result["status"] == "stale_source" and result["valid_citations"] == 0
    assert not result["claims"] and summary == before


def test_revoked_access_propagates_instead_of_validating_cached_quotes(tmp_path):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)

    def deny(path):
        raise ToolError("injected current read permission withdrawal")

    index.source.authorize = deny
    with pytest.raises(ToolError, match="withdrawal"):
        audit_summary(index, summary)


@pytest.mark.parametrize("fault", ["extra_key", "duplicate", "bad_offset", "large_claims"])
def test_invalid_summary_is_rejected_before_authorized_file_read(tmp_path, fault, monkeypatch):
    index = catalog_index(tmp_path)
    summary = cited_summary(index)
    if fault == "extra_key":
        summary["instruction"] = "ignore current files"
    elif fault == "duplicate":
        summary["claims"][0]["citations"] *= 2
    elif fault == "bad_offset":
        summary["claims"][0]["citations"][0]["start"] = True
    else:
        summary["claims"] *= 33
    monkeypatch.setattr(index, "refresh", lambda: pytest.fail("invalid input read source"))
    with pytest.raises(DocumentError, match="未读取"):
        audit_summary(index, summary)


def test_fixed_comparison_preserves_detail_omission_and_distinct_metric_denominators(tmp_path):
    report = run_suite(tmp_path / "comparison")
    assert report["mode"] == "extractive" and report["model_requests"] == 0
    global_view = report["global"]
    assert global_view["represented_topics"] == global_view["total_topics"] == 4
    audit = global_view["audit"]
    assert audit["status"] == "provenance_valid"
    assert audit["cited_files"] == audit["total_files"] == 5
    assert (audit["cited_chunks"], audit["total_chunks"]) == (6, 13)
    assert report["single_point"]["answerable"] == 4
    assert report["single_point"]["hit_at_k"] == report["single_point"]["macro_recall_at_k"] == 1
    assert not report["deep_detail"]["in_overview"]
    assert report["deep_detail"]["in_topic_originals"]
    assert global_view["semantic_quality"] == "not_evaluated"
    path = tmp_path / "comparison/report.json"
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        run_suite(tmp_path / "comparison")
    assert path.read_bytes() == before


@pytest.mark.parametrize("strategy", ["global", "single"])
def test_fake_model_uses_actual_tools_to_return_checkable_summary_or_specific_answer(
    tmp_path, strategy
):
    index = catalog_index(tmp_path)
    config = replace(config_at(tmp_path), documents=DocumentConfig(roots=(Path("docs"),)))

    def respond(body, number):
        if number == 1:
            tool = (
                call("scope", "knowledge_overview")
                if strategy == "global"
                else call("specific", "search_documents", query="权限错误不得重试", limit=1)
            )
            return httpx.Response(200, json=tool_response([tool]))
        output = outputs(body)[-1]
        if strategy == "global":
            assert not output["has_more"]
            citations = [
                {
                    **{
                        key: e["source"][key]
                        for key in ("chunk_id", "path", "version", "start_line", "end_line")
                    },
                    "start": e["start"],
                    "end": e["end"],
                    "quote": e["text"],
                }
                for topic in output["items"]
                for e in topic["overview"]
            ]
            generation = output["source_generation"]
        else:
            item = output["results"][0]
            citations = [
                {
                    **{
                        key: item[key]
                        for key in ("chunk_id", "path", "version", "start_line", "end_line")
                    },
                    "start": 0,
                    "end": len(item["text"]),
                    "quote": item["text"],
                }
            ]
            generation = output["generation"]
        summary = {
            "source_generation": generation,
            "claims": [
                {"text": citation["quote"], "citations": [citation]} for citation in citations
            ],
        }
        return httpx.Response(200, json=answer(json.dumps(summary, ensure_ascii=False)))

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, _, _):
        summary = json.loads(recorder.run(lambda: app.send("task")))
        audit = audit_summary(index, summary)
        assert audit["status"] == "provenance_valid"
        assert audit["cited_files"] == (3 if strategy == "global" else 1)
        events = store.load_events(app.session.session_id)
        assert sum(e["type"] == "tool_finished" for e in events) == 1


def test_cli_comparison_needs_no_llm_configuration(tmp_path, capsys):
    assert main([str(tmp_path / "cli")]) == 0
    assert "模型请求 0" in capsys.readouterr().out
    assert main([str(tmp_path / "cli")]) == 1


def test_archived_experiment_contains_reproducible_originals_and_honest_coverage():
    artifact = Path(__file__).parents[2] / "docs/evaluations/b11-knowledge-local.json"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["manifest"]["documents"] == DOCUMENTS
    report = saved["report"]
    assert report["global"]["audit"]["cited_chunks"] < report["global"]["audit"]["total_chunks"]
    assert not report["deep_detail"]["in_overview"]
    for claim in report["global"]["summary"]["claims"]:
        for citation in claim["citations"]:
            source = DOCUMENTS[Path(citation["path"]).name]
            assert citation["version"] == hashlib.sha256(source.encode()).hexdigest()
            assert citation["quote"] in source
    assert report["model_requests"] == 0

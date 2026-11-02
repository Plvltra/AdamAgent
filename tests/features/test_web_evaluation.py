"""C01.4：仅模拟模型 HTTP，真实应用与重定向站点；报告不把结构有效当语义正确。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.llm.http_transport import InterruptibleTransport
from adam.web import runtime
from adam.web.evidence import page_result
from adam.web.models import WebPage
from scripts import web_evaluation as evaluation
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call, outputs


def wire(calls=None, text=None, *, usage=True):
    delta = {"role": "assistant", "content": text or ""}
    if calls:
        delta["tool_calls"] = [
            {
                "index": i,
                "id": identity,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            for i, (identity, name, args) in enumerate(calls)
        ]
    return sse(
        [
            event(delta),
            event(reason="tool_calls" if calls else "stop", usage=USAGE if usage else None),
            DONE,
        ]
    )


@pytest.fixture
def model_http(monkeypatch):
    """只在正式模型传输层替换 HTTP；网页仍使用真实本地连接。"""
    bodies = []
    responder = {}

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.url.host == "127.0.0.1" and request.url.path == "/v1/chat/completions"
        return responder["run"](body, len(bodies))

    monkeypatch.setattr(
        InterruptibleTransport, "handle_request", lambda _, request: handle(request)
    )
    return bodies, responder


def test_redirect_run_uses_production_turn_and_exports_actual_source(tmp_path, model_http):
    bodies, responder = model_http
    original_reader = runtime.open_reader
    with evaluation.site() as (origin, paths):

        def respond(body, number):
            if number == 1:
                return wire([call("discover", "discover_tools", names=["read_webpage"])])
            if number == 2:
                return wire([call("read", "read_webpage", url=origin + "/latest")])
            output = outputs(body)[-1]
            return wire(text="TERN-482 [" + output["excerpt"]["citation"] + "]")

        responder["run"] = respond
        item = evaluation.run_case(
            tmp_path, evaluation.CASES[3], "web", "http://127.0.0.1:8087/v1", "test", origin, paths
        )
        assert item["transport_and_turn_success"] and len(bodies) == 3
        assert item["local_http_requests"] == ["/latest", "/releases/r7"]
        evidence = item["evidence"]
        assert len(evidence["sources"]) == len(evidence["citations"]) == 1
        assert evidence["citations"][0]["range_valid"]
        assert evidence["citations"][0]["semantic_support"] == "requires_human_review"
        source = next(iter(evidence["sources"].values()))
        assert source["final_url"] == origin + "/releases/r7"
        assert [t["tool"]["name"] for t in evidence["tool_results"]] == [
            "discover_tools",
            "read_webpage",
        ]
        report = item["diagnostics"][0]
        assert len(report["turns"]) == 1 and len(report["turns"][0]["requests"]) == 3
        assert not report["unknown_usage"] and item["cost"] is None
        saved = json.loads((tmp_path / "runs/redirect/web/result.json").read_text(encoding="utf-8"))
        assert saved["answer"] == item["answer"]
        assert saved["physical_model_requests"] == 3
        assert saved["evidence"]["citations"] == evidence["citations"]
        with pytest.raises(FileExistsError):
            evaluation.run_case(
                tmp_path,
                evaluation.CASES[3],
                "web",
                "http://127.0.0.1:8087/v1",
                "test",
                origin,
                paths,
            )
        assert len(bodies) == 3
    assert runtime.open_reader is original_reader


@pytest.mark.parametrize("arm", ["baseline", "web"])
@pytest.mark.parametrize("usage", [True, False])
def test_plain_question_keeps_unknown_usage_and_never_contacts_site(
    tmp_path, model_http, arm, usage
):
    bodies, responder = model_http
    responder["run"] = lambda *_: wire(text="Absolute value is idempotent.", usage=usage)
    with evaluation.site() as (origin, paths):
        item = evaluation.run_case(
            tmp_path, evaluation.CASES[4], arm, "http://127.0.0.1:8087/v1", "test", origin, paths
        )
    assert item["transport_and_turn_success"] is usage
    assert len(bodies) == 1 and paths == []
    assert item["diagnostics"][0]["unknown_usage"] is (not usage)
    counts = item["diagnostics"][0]["turns"][0]["requests"][0]["usage"]
    assert counts == (
        {"input_tokens": 10, "output_tokens": 3}
        if usage
        else {"input_tokens": None, "output_tokens": None}
    )
    assert item["evidence"]["citations"] == [] and item["cost"] is None


def test_model_http_failure_is_preserved_without_retry_or_fake_answer(tmp_path, model_http):
    bodies, responder = model_http
    responder["run"] = lambda *_: httpx.Response(503, text="temporary")
    with evaluation.site() as (origin, paths):
        item = evaluation.run_case(
            tmp_path, evaluation.CASES[0], "web", "http://127.0.0.1:8087/v1", "test", origin, paths
        )
    assert not item["transport_and_turn_success"] and item["answer"] is None
    assert item["error"]["type"] == "LLMError" and len(bodies) == 1
    assert not item["local_http_requests"] and not item["evidence"]["sources"]
    assert item["diagnostics"][0]["turns"][0]["status"] == "failed"


def test_prepare_is_fixed_and_invalid_endpoint_or_modified_batch_cannot_call_model(
    tmp_path, model_http
):
    bodies, _ = model_http
    root = tmp_path / "batch"
    evaluation.prepare(root)
    frozen = (root / "fixed.json").read_bytes()
    with pytest.raises(FileExistsError):
        evaluation.prepare(root)
    assert (root / "fixed.json").read_bytes() == frozen
    with pytest.raises(ValueError):
        evaluation.evaluate(root, "https://provider.example/v1", "test", tmp_path / "missing")
    data = json.loads(frozen)
    data["cases"][0]["question"] = "retuned"
    (root / "fixed.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Frozen batch"):
        evaluation.evaluate(root, "http://127.0.0.1:8087/v1", "test", tmp_path / "missing")
    assert bodies == [] and not (root / "runs").exists()


def test_comparison_configuration_changes_only_web_capability(tmp_path):
    baseline = evaluation.comparison_config(
        tmp_path, "baseline", "http://127.0.0.1:8087/v1", "test"
    )
    web = evaluation.comparison_config(tmp_path, "web", "http://127.0.0.1:8087/v1", "test")
    assert replace(web, web=baseline.web) == baseline
    assert baseline.llm.api_key == "local-evaluation-not-a-secret"
    assert baseline.rules.enabled is False and baseline.reflection.enabled is False


def test_source_range_validation_never_scores_semantics_or_accepts_another_tool():
    page = WebPage(
        "https://example.org/",
        "https://example.org/",
        "2026-11-02T00:00:00+00:00",
        "a" * 64,
        20,
        "Test",
        "Evidence says 42.",
        "text/html",
        False,
        (),
    )
    output = page_result(page, 8000)
    start = {"type": "tool_started", "payload": {"tool_call_id": "read", "name": "read_webpage"}}
    finish = {
        "type": "tool_finished",
        "payload": {"tool_call_id": "read", "output": json.dumps(output)},
    }
    valid = output["excerpt"]["citation"]
    answer = (
        "incorrect 999 ["
        + valid
        + "] ["
        + output["source_id"]
        + ":0-999] [web-"
        + "b" * 64
        + ":0-4]"
    )
    report = evaluation.citation_report(answer, [start, finish])
    assert [c["range_valid"] for c in report["citations"]] == [True, False, False]
    assert all(c["semantic_support"] == "requires_human_review" for c in report["citations"])
    start["payload"]["name"] = "search_web"
    assert not evaluation.citation_report(answer, [start, finish])["sources"]

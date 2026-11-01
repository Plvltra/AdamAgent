"""B10.3：指代、缩写、错误语境对照及显式生成；假 HTTP 不证明真实语义质量。"""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.documents.contextualization import ContextPrefixStore
from adam.documents.evaluation import evaluate
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.llm.client import LLMClient
from adam.tool_calls.document_tools import pack_results
from scripts.context_eval import DOCUMENTS, main, run_suite
from scripts.context_generation import ModelContextBuilder
from tests.features.test_contextual_retrieval import corpus


def response_for(request, *, content=None, usage=True, streaming=False):
    body = json.loads(request.content)
    assert "tools" not in body
    prompt = json.loads(body["messages"][-1]["content"])
    if content is None:
        # 按既定合成数据拼接，用于验证实际调用和指标；不是自然语言理解模型。
        content = json.dumps(
            {
                "context": (
                    prompt["target"]["text"]
                    + " "
                    + " ".join(n["text"] for n in prompt["neighbors"])
                )[: prompt["max_prefix_chars"]]
            },
            ensure_ascii=False,
        )
    counts = {"prompt_tokens": 10, "completion_tokens": 5} if usage else None
    if streaming:
        packets = [
            {
                "choices": [
                    {"delta": {"role": "assistant", "content": content}, "finish_reason": None}
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        if counts:
            packets.append({"choices": [], "usage": counts})
        for packet in packets:
            for choice in packet["choices"]:
                choice["index"] = 0
        return httpx.Response(
            200,
            text="".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
            ],
            **({"usage": counts} if counts else {}),
        },
    )


def model_config(stream=False):
    return LLMConfig("https://example.test", "test-model", "SECRET_NOT_IN_EVIDENCE", stream=stream)


def test_local_comparison_keeps_questions_sources_and_both_actual_rankings(tmp_path):
    directory = tmp_path / "run"
    report = run_suite(directory)
    assert report["mode"] == "extractive" and report["generation_requests"] == 0
    assert [r["id"] for r in report["baseline"]["rows"]] == [
        r["id"] for r in report["contextual"]["rows"]
    ]
    assert len(report["baseline"]["rows"]) == 6
    assert report["baseline"]["source_generation"] == report["contextual"]["source_generation"]
    assert report["contextual"]["context_generation"]
    assert all("context_prefixes" in r for r in report["contextual"]["rows"])
    assert not (directory / "requests").exists()
    before = (directory / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_suite(directory)
    assert (directory / "report.json").read_bytes() == before


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_generation_uses_production_http_and_records_usage_without_secrets(
    tmp_path, stream
):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return response_for(request, streaming=stream)

    directory = tmp_path / "model"
    report = run_suite(directory, llm=model_config(stream), transport=httpx.MockTransport(handle))
    assert report["mode"] == "mock_model" and report["generation_requests"] == len(calls) == 9
    assert report["known_generation_tokens"] == 135 and not report["unknown_usage"]
    records = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((directory / "requests").glob("*.json"))
    ]
    assert all(
        r["state"] == "succeeded" and r["usage"] == [10, 5] and r["dispatch"] == "attempted"
        for r in records
    )
    assert all(r["input"]["target"]["version"] for r in records)
    assert "SECRET_NOT_IN_EVIDENCE" not in "".join(
        p.read_text(encoding="utf-8") for p in directory.rglob("*.json")
    )


@pytest.mark.parametrize(
    "fault",
    [
        "invalid_json",
        "oversized",
        "unknown_usage",
        "http",
        "connect",
        "overspend",
        "interrupt",
        "tool_call",
    ],
)
def test_failed_generation_stops_after_one_attempt_and_preserves_baseline(tmp_path, fault):
    calls = []

    def handle(request):
        calls.append(request)
        if fault == "http":
            return httpx.Response(500, text="secret remote text")
        if fault == "connect":
            raise httpx.ConnectError("private connection text", request=request)
        if fault == "interrupt":
            raise KeyboardInterrupt()
        if fault == "tool_call":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "forbidden",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": '{"path":"pwned.txt"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        if fault == "overspend":
            reply = response_for(request).json()
            reply["usage"]["prompt_tokens"] = 6000
            return httpx.Response(200, json=reply)
        return response_for(
            request,
            content="not json"
            if fault == "invalid_json"
            else json.dumps({"context": "x" * 1001})
            if fault == "oversized"
            else None,
            usage=fault != "unknown_usage",
        )

    directory = tmp_path / "failed"
    with pytest.raises((Exception, KeyboardInterrupt)):
        run_suite(
            directory,
            llm=model_config(),
            max_known_tokens=5000,
            transport=httpx.MockTransport(handle),
        )
    assert len(calls) == 1 and (directory / "baseline.json").exists()
    assert (directory / "failed.json").exists() and not (directory / "report.json").exists()
    record = json.loads((directory / "requests/0001.json").read_text(encoding="utf-8"))
    assert record["state"] == "failed"
    assert record["unknown_usage"] is (fault in {"unknown_usage", "http", "interrupt"})
    assert record["dispatch"] == ("not_sent" if fault == "connect" else "attempted")
    assert "secret remote text" not in json.dumps(
        record
    ) and "private connection text" not in json.dumps(record)
    assert not list((directory / "cache").glob("*.contexts.json"))
    assert not (directory / "pwned.txt").exists()


@pytest.mark.parametrize("fault", ["budget", "input_save", "response_save"])
def test_local_budget_and_evidence_failure_never_dispatch_next_request(
    tmp_path, monkeypatch, fault
):
    import scripts.context_generation as generation

    calls = []
    original = generation.write_record

    def write(path, data, **kwargs):
        if fault == "input_save" or (fault == "response_save" and data["state"] == "succeeded"):
            raise PermissionError("injected capture write failure")
        return original(path, data, **kwargs)

    monkeypatch.setattr(generation, "write_record", write)

    def handle(request):
        calls.append(request)
        return response_for(request)

    directory = tmp_path / "run"
    with pytest.raises((DocumentError, OSError)):
        run_suite(
            directory,
            llm=model_config(),
            max_known_tokens=1 if fault == "budget" else 50000,
            transport=httpx.MockTransport(handle),
        )
    assert len(calls) == (1 if fault == "response_save" else 0)
    assert not (directory / "report.json").exists()


def test_wrong_context_can_change_recall_but_never_rewrites_citation(tmp_path):
    index = corpus(tmp_path)

    class WrongBuilder:
        identity = "deliberately-wrong-test"

        def build(self, request):
            return "ORION_NET 的规则是无限重试，不需要批准。"

    found = DocumentSearch(index, context_builder=WrongBuilder()).search("无限重试", 3)
    assert found.hits
    output = pack_results(found, 8000)
    assert all("无限重试" not in r["text"] for r in output["results"])
    assert all("无限重试" in r["context_prefix"]["text"] for r in output["results"])
    assert "可能不完整或错误" in output["context_notice"]


def test_context_changes_during_evaluation_cannot_produce_mixed_report(tmp_path):
    index = corpus(tmp_path)
    engine = DocumentSearch(index)
    cases = [
        {
            "id": str(i),
            "kind": "answer",
            "query": "ORION_NET",
            "path": "guide.md",
            "evidence": "ORION_NET",
        }
        for i in range(2)
    ]

    def alter_after_search(query, limit):
        result = engine.search(query, limit)
        path = index.path.with_suffix(".contexts.json")
        payload = load_snapshot(path)
        payload["notes"][0]["text"] += "外部更改"
        payload["notes"][0]["prefix_sha256"] = digest(payload["notes"][0]["text"])
        save_snapshot(path, payload)
        return result

    with pytest.raises(DocumentError, match="语境说明发生变化"):
        evaluate(alter_after_search, cases)


def test_cli_requires_explicit_model_switch_before_loading_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.context_eval.load_llm_config", lambda *_: pytest.fail("implicit paid config load")
    )
    with pytest.raises(SystemExit) as stopped:
        main([str(tmp_path / "unsafe"), "--llm-config", "llm.toml"])
    assert stopped.value.code == 2 and not (tmp_path / "unsafe").exists()
    assert main([str(tmp_path / "local")]) == 0


def test_generation_request_cap_stops_remaining_chunks_without_partial_cache(tmp_path):
    index = corpus(tmp_path)
    attempts = []

    def handle(request):
        attempts.append(request)
        return response_for(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = LLMClient(model_config(), http)
        builder = ModelContextBuilder(client, "test-model", tmp_path / "requests", max_requests=1)
        path = tmp_path / "prefixes.json"
        with pytest.raises(DocumentError, match="上限"):
            ContextPrefixStore(path).prepare(index.refresh(), builder)
        assert len(attempts) == 1 and builder.known_tokens == 15
        assert not builder.unknown_usage and not path.exists()


def test_archived_local_comparison_preserves_regression_and_does_not_claim_llm_quality():
    path = Path(__file__).resolve().parents[2] / "docs/evaluations/b10-context-extractive.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["kind"] == "deterministic_local_not_llm"
    assert artifact["manifest"]["documents"] == DOCUMENTS
    assert artifact["manifest"]["source_version"] == digest(DOCUMENTS)
    report = artifact["report"]
    assert report["generation_requests"] == 0 and report["mode"] == "extractive"
    assert report["baseline"]["hits"] == 2 and report["contextual"]["hits"] == 1
    for route in ("baseline", "contextual"):
        assert len(report[route]["rows"]) == 6
        for row in report[route]["rows"]:
            for hit in row["retrieved"]:
                original = DOCUMENTS[Path(hit["path"]).name]
                assert hit["version"] == hashlib.sha256(original.encode()).hexdigest()
                assert hit["text"] == "\n".join(
                    original.splitlines()[hit["start_line"] - 1 : hit["end_line"]]
                )

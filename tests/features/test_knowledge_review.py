"""B12.2.2：模拟 HTTP，实际走独立请求留证、可靠用量、候选与发布状态。"""

import copy
import json

import httpx
import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError, RequestInterrupted
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.knowledge_review import KnowledgeModelReviewer
from adam.documents.models import DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.llm.client import LLMClient
from tests.features.test_context_prefix_evaluation import model_config, response_for
from tests.features.test_knowledge_candidates import proposal_fixture


@pytest.fixture
def knowledge(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    return KnowledgeLifecycle(store), store.create(proposal)


def opinion_for(request):
    proposal = json.loads(json.loads(request.content)["messages"][-1]["content"])["proposal"]
    return {
        "verdict": "accept",
        "summary": "仅采用有时间范围的新规则，需用户核对。",
        "counterevidence_assessment": "旧规则仍可能适用于旧版本，不得从新规则反推旧版本。",
        "evidence_ids": sorted(
            {c["chunk_id"] for c in proposal["support"] + proposal["counterevidence"]}
        ),
    }


@pytest.mark.parametrize("stream", [False, True])
def test_independent_http_review_records_intent_and_usage_but_never_publishes(knowledge, stream):
    lifecycle, candidate = knowledge
    directory = lifecycle.proposals.root / "reviews"
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "tools" not in body and len(body["messages"]) == 2
        sent = json.loads(body["messages"][-1]["content"])
        assert sent["proposal"] == candidate["proposal"]
        records = list(directory.glob("*.json"))
        assert len(records) == 1
        intent = load_snapshot(records[0])
        assert intent["state"] == "started" and intent["dispatch"] == "intent"
        assert intent["unknown_usage"] and intent["usage"] is None
        return response_for(request, content=json.dumps(opinion_for(request)), streaming=stream)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        reviewer = KnowledgeModelReviewer(
            LLMClient(model_config(stream), http), "review", directory
        )
        state = lifecycle.review(candidate["candidate_id"], reviewer)["state"]
        assert state["status"] == "reviewed" and state["reviewer"] == reviewer.identity
        with pytest.raises(DocumentError, match="已经使用"):
            reviewer.review(candidate, "0" * 16)
    record = load_snapshot(directory / (state["review_id"] + ".json"))
    assert record["state"] == "succeeded" and record["usage"] == [10, 5]
    assert not record["unknown_usage"] and record["elapsed_ms"] >= 0
    assert record["candidate_hash"] == candidate["content_hash"]
    assert json.loads(record["response"]) == state["review"]
    assert len(calls) == 1 and calls[0]["max_tokens"] == 1024
    assert "SECRET_NOT_IN_EVIDENCE" not in json.dumps(record)
    assert lifecycle.current() == []
    assert (
        lifecycle.publish(candidate["candidate_id"], lambda view: False)["state"]["status"]
        == "rejected"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "approval",
        "counter",
        "oversized",
        "unknown",
        "http",
        "connect",
        "overbudget",
        "interrupt",
        "tool",
    ],
)
def test_remote_failure_preserves_usage_and_never_retries_or_adopts_opinion(knowledge, fault):
    lifecycle, candidate = knowledge
    directory = lifecycle.proposals.root / "reviews"
    calls = []

    def handle(request):
        calls.append(request)
        if fault == "http":
            return httpx.Response(503, text="PRIVATE_REMOTE_ERROR")
        if fault == "connect":
            raise httpx.ConnectError("PRIVATE_CONNECTION_DETAIL", request=request)
        if fault == "interrupt":
            raise KeyboardInterrupt()
        opinion = opinion_for(request)
        if fault == "approval":
            opinion["approved"] = True
        if fault == "counter":
            opinion["evidence_ids"] = opinion["evidence_ids"][:1]
        content = (
            "not-json"
            if fault == "json"
            else "x" * 10001
            if fault == "oversized"
            else json.dumps(opinion)
        )
        response = response_for(request, content=content, usage=fault != "unknown")
        payload = response.json()
        if fault == "overbudget":
            payload["usage"]["prompt_tokens"] = 60000
        if fault == "tool":
            payload["choices"][0] = {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "forbidden",
                            "type": "function",
                            "function": {
                                "name": "publish_knowledge",
                                "arguments": '{"approved":true}',
                            },
                        }
                    ],
                },
            }
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        reviewer = KnowledgeModelReviewer(LLMClient(model_config(), http), "review", directory)
        with pytest.raises((AgentError, KeyboardInterrupt)):
            lifecycle.review(candidate["candidate_id"], reviewer)
        with pytest.raises(DocumentError, match="已经使用"):
            lifecycle.review(candidate["candidate_id"], reviewer)
    record = load_snapshot(next(directory.glob("*.json")))
    assert record["state"] == "failed" and len(calls) == 1
    assert record["dispatch"] == ("not_sent" if fault == "connect" else "attempted")
    assert record["unknown_usage"] is (fault in {"unknown", "http", "interrupt"})
    expected_usage = (
        None
        if record["unknown_usage"]
        else [0, 0]
        if fault == "connect"
        else [60000, 5]
        if fault == "overbudget"
        else [10, 5]
    )
    assert record["usage"] == expected_usage
    assert record["response_truncated"] is (fault == "oversized")
    assert "PRIVATE_" not in json.dumps(record)
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "pending"
    assert lifecycle.current() == []


@pytest.mark.parametrize("fault", ["input", "response", "permanent"])
def test_record_write_failure_does_not_report_review_success(knowledge, monkeypatch, fault):
    lifecycle, candidate = knowledge
    directory = lifecycle.proposals.root / "reviews"
    calls = []
    original = save_snapshot

    def save(path, record):
        if fault == "input" or record["state"] == "succeeded" or (fault == "permanent" and calls):
            raise PermissionError("injected review evidence storage failure")
        original(path, record)

    monkeypatch.setattr("adam.documents.knowledge_review.save_snapshot", save)

    def handle(request):
        calls.append(request)
        return response_for(request, content=json.dumps(opinion_for(request)))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        reviewer = KnowledgeModelReviewer(LLMClient(model_config(), http), "review", directory)
        with pytest.raises(DocumentError, match="记录读写失败"):
            lifecycle.review(candidate["candidate_id"], reviewer)
    assert len(calls) == (0 if fault == "input" else 1)
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "pending"
    paths = list(directory.glob("*.json"))
    if fault == "input":
        assert paths == []
    else:
        record = load_snapshot(paths[0])
        assert record["state"] == ("started" if fault == "permanent" else "failed")
        assert record["unknown_usage"] is (fault == "permanent")


@pytest.mark.parametrize("when", ["before", "intent", "response"])
def test_cancel_before_or_after_dispatch_preserves_known_consumption(knowledge, monkeypatch, when):
    lifecycle, candidate = knowledge
    directory = lifecycle.proposals.root / "reviews"
    token = CancellationToken()
    calls = []
    if when == "before":
        token.cancel()
    if when == "intent":

        def cancel_after_intent(path, record):
            save_snapshot(path, record)
            if record["state"] == "started":
                token.cancel()

        monkeypatch.setattr("adam.documents.knowledge_review.save_snapshot", cancel_after_intent)

    def handle(request):
        calls.append(request)
        token.cancel()
        return response_for(request, content=json.dumps(opinion_for(request)))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        reviewer = KnowledgeModelReviewer(
            LLMClient(model_config(), http), "review", directory, check_cancel=token.check
        )
        with pytest.raises(RequestInterrupted):
            lifecycle.review(candidate["candidate_id"], reviewer)
    assert len(calls) == (1 if when == "response" else 0)
    paths = list(directory.glob("*.json"))
    if when == "before":
        assert paths == []
    else:
        record = load_snapshot(paths[0])
        assert record["usage"] == ([10, 5] if when == "response" else [0, 0])
        assert record["state"] == "failed" and not record["unknown_usage"]
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "pending"


def test_request_success_does_not_adopt_stale_source_after_model_returns(knowledge, tmp_path):
    lifecycle, candidate = knowledge
    directory = lifecycle.proposals.root / "reviews"

    def handle(request):
        (tmp_path / "docs/new.md").write_text("新规则改为三次。", encoding="utf-8")
        return response_for(request, content=json.dumps(opinion_for(request)))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        reviewer = KnowledgeModelReviewer(LLMClient(model_config(), http), "review", directory)
        with pytest.raises(DocumentError):
            lifecycle.review(candidate["candidate_id"], reviewer)
    record = load_snapshot(next(directory.glob("*.json")))
    assert record["state"] == "succeeded" and record["usage"] == [10, 5]
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "pending"
    assert lifecycle.history(candidate["candidate_id"]) == []


@pytest.mark.parametrize("fault", ["budget", "input_size", "hash", "request_id", "existing"])
def test_preflight_refuses_invalid_or_repeated_request_without_http(knowledge, fault):
    lifecycle, original = knowledge
    candidate = copy.deepcopy(original)
    directory = lifecycle.proposals.root / "reviews"
    review_id = "a" * 16
    if fault == "input_size":
        # 构造格式合法的大载荷，仅检验适配器在发送前的体积边界；不冒充真实源引用。
        quote = candidate["proposal"]["support"][0]
        candidate["proposal"]["support"] = [
            {**quote, "chunk_id": f"{i:016x}", "quote": "字" * 3000, "end": 3000} for i in range(8)
        ]
        candidate["content_hash"] = digest(
            {k: v for k, v in candidate.items() if k != "content_hash"}
        )
    elif fault == "hash":
        candidate["proposal"]["statement"] = "内容被换过"
    elif fault == "request_id":
        review_id = "../escape"
    elif fault == "existing":
        directory.mkdir()
        save_snapshot(directory / (review_id + ".json"), {"state": "started"})
    before = {p: p.read_bytes() for p in directory.glob("*.json")}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch"))
    ) as http:
        reviewer = KnowledgeModelReviewer(
            LLMClient(model_config(), http),
            "review",
            directory,
            max_known_tokens=1 if fault == "budget" else 50000,
        )
        with pytest.raises(DocumentError):
            reviewer.review(candidate, review_id)
    assert {p: p.read_bytes() for p in directory.glob("*.json")} == before

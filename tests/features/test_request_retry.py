"""V5.3：仅未发出的连接失败重试；每次尝试独立留痕，失败不绕过预算或日志边界。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import AgentConfig, ContextConfig, LLMConfig
from adam.contracts.errors import BudgetError, LLMError, RequestNotSentError, StorageError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.support import build_conversation


@pytest.fixture
def retry_chat(tmp_path):
    responses, requests, notices = [], [], []

    def handle(request):
        requests.append(json.loads(request.content))
        item = responses.pop(0) if responses else success()
        if isinstance(item, Exception):
            raise item
        return item

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        client = LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http)
        conversation = build_conversation(store, client, "", notify=notices.append)
        yield conversation, responses, requests, notices


def success():
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    )


@pytest.mark.parametrize("stream", [False, True])
def test_connection_retry_succeeds_and_records_each_physical_request(retry_chat, stream):
    conversation, responses, requests, notices = retry_chat
    conversation.client.config = replace(conversation.client.config, stream=stream)
    responses.extend([httpx.ConnectError("private"), httpx.ConnectTimeout("private")])
    responses.append(
        sse([event({"content": "done"}, reason="stop", usage=USAGE), DONE]) if stream else success()
    )
    assert conversation.send("task") == "done"
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    turn = report["turns"][0]
    assert turn["status"] == "succeeded" and len(turn["requests"]) == 3
    assert [item["reason"] for item in turn["requests"]] == [
        "api_not_sent",
        "api_not_sent",
        "completed",
    ]
    assert turn["requests"][0]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert not report["unknown_usage"] and len(notices) == 2
    assert (
        len(requests) == 3
        and len(conversation.store.load_messages(conversation.session.session_id)) == 2
    )


@pytest.mark.parametrize("limit,count,code", [(8, 3, "api_not_sent"), (2, 2, "request_limit")])
def test_retry_exhaustion_and_request_budget(retry_chat, limit, count, code):
    conversation, responses, requests, _ = retry_chat
    conversation.limits = AgentConfig(max_model_requests=limit)
    responses.extend([httpx.ConnectError("private")] * 3)
    with pytest.raises((RequestNotSentError, BudgetError)) as error:
        conversation.send("task")
    assert error.value.code == code and len(requests) == count
    assert conversation.store.usage(conversation.session.session_id) == (0, False)
    assert conversation.store.load_messages(conversation.session.session_id) == []


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("private"),
        httpx.ReadError("private"),
        httpx.Response(401),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(200, text="invalid"),
    ],
)
def test_uncertain_or_server_failure_never_retries(retry_chat, failure):
    conversation, responses, requests, notices = retry_chat
    responses.append(failure)
    with pytest.raises(LLMError):
        conversation.send("task")
    assert len(requests) == 1 and notices == []
    assert conversation.store.usage(conversation.session.session_id)[1]


def test_connect_error_after_response_began_is_not_retried(retry_chat):
    conversation, responses, requests, _ = retry_chat
    conversation.client.config = replace(conversation.client.config, stream=True)
    responses.append(sse([event({"content": "partial"}), httpx.ConnectError("late")]))
    with pytest.raises(LLMError) as caught:
        conversation.send("task")
    assert caught.value.code == "api_connection" and len(requests) == 1


def test_retry_does_not_repeat_an_already_completed_command(tmp_path, retry_chat):
    conversation, responses, requests, _ = retry_chat
    target = tmp_path / "once.txt"
    conversation.tool_executor.registry = create_tool_registry(
        tmp_path, confirm_command=lambda _: True
    )
    args = {
        "argv": [
            sys.executable,
            "-c",
            "from pathlib import Path; p=Path('once.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
        ]
    }
    responses.extend(
        [
            httpx.Response(
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
                                        "id": "command",
                                        "type": "function",
                                        "function": {
                                            "name": "run_command",
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            ),
            httpx.ConnectError("private"),
            success(),
        ]
    )
    assert conversation.send("execute once") == "done"
    assert target.read_text() == "x" and len(requests) == 3
    assert len(conversation.store.load_executions(conversation.session.session_id)) == 1


def test_failed_settlement_cannot_trigger_retry(retry_chat, monkeypatch):
    conversation, responses, requests, notices = retry_chat
    responses.append(httpx.ConnectError("private"))

    def fail(*args, **kwargs):
        raise StorageError("injected settlement failure")

    monkeypatch.setattr(conversation.store, "finish_request", fail)
    with pytest.raises(StorageError):
        conversation.send("task")
    assert len(requests) == 1 and notices == []
    assert conversation.store.usage(conversation.session.session_id)[1]


@pytest.mark.parametrize("automatic", [False, True])
def test_summary_retries_are_separate_operations_with_shared_budget(retry_chat, automatic):
    conversation, responses, requests, _ = retry_chat
    session_id = conversation.new().session_id
    for index in range(8):
        conversation.store.append_turn(session_id, f"step {index}", "中" * 1000)
    if automatic:
        conversation.context_builder.limits = ContextConfig(
            max_input_tokens=9000, safety_margin_tokens=0
        )
        conversation.limits = AgentConfig(max_model_requests=3)
    responses.extend([httpx.ConnectError("private"), success(), success()])
    if automatic:
        assert conversation.send("next") == "done"
    else:
        assert conversation.compact() is not None
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    assert [item["status"] for item in report["compactions"]] == ["failed", "succeeded"]
    assert report["compactions"][0]["request"]["reason"] == "api_not_sent"
    assert report["compactions"][0]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert len(requests) == (3 if automatic else 2)
    assert not report["unknown_usage"]

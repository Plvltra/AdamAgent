"""Messages的TOML、HTTP与真实文件工具闭环；远端响应使用MockTransport。"""

import json

import httpx
import pytest

from adam.config.loader import load_llm_config
from adam.config.settings import LLMConfig
from adam.contracts.errors import ConfigError, LLMError
from adam.llm.client import LLMClient
from adam.llm.messages_protocol import decode_message, encode_messages
from adam.llm.messages_usage import parse_messages_usage
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def test_messages_rejects_private_history_before_http_and_does_not_retry_http_error():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, text="PRIVATE_REMOTE_DETAIL")

    config = LLMConfig(
        "https://test.invalid", "test", "test-only", stream=False, protocol="messages"
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = LLMClient(config, http)
        with pytest.raises(LLMError) as rejected:
            client.complete(
                [{"role": "assistant", "content": "old", "reasoning_content": "private"}]
            )
        assert rejected.value.usage == (0, 0) and not requests
        with pytest.raises(LLMError) as failed:
            client.complete([{"role": "user", "content": "hello"}])
        assert failed.value.usage is None and failed.value.http_status == 503
        assert "PRIVATE_REMOTE_DETAIL" not in str(failed.value) and len(requests) == 1


def test_messages_requires_explicit_nonstream_and_known_protocol():
    for options in ({"protocol": "messages"}, {"protocol": "unknown"}, {"protocol": []}):
        with pytest.raises(ConfigError):
            LLMConfig("https://test.invalid", "test", "test-only", **options)


def test_messages_runs_real_file_tool_and_restores_history(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("value = 42\n", encoding="utf-8")
    bodies = []
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "output_tokens": 3,
    }

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert "read_file" in {tool["name"] for tool in body["tools"]}
            content = [
                {
                    "type": "tool_use",
                    "id": "read_1",
                    "name": "read_file",
                    "input": {"path": "sample.py"},
                }
            ]
            reason = "tool_use"
        else:
            tool_results = [
                block
                for message in body["messages"]
                for block in message["content"]
                if block["type"] == "tool_result"
            ]
            assert tool_results[0]["tool_use_id"] == "read_1"
            assert "value = 42" in tool_results[0]["content"]
            content = [{"type": "text", "text": "value is 42"}]
            reason = "end_turn"
        return httpx.Response(
            200,
            json={"role": "assistant", "content": content, "stop_reason": reason, "usage": usage},
        )

    config = LLMConfig(
        "https://test.invalid/anthropic", "test", "test-only", stream=False, protocol="messages"
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with SessionStore(tmp_path / "sessions") as store:
            conversation = build_conversation(
                store, LLMClient(config, http), "rules", create_tool_registry(workspace)
            )
            assert conversation.send("read sample.py") == "value is 42"
            session_id = conversation.session.session_id
            assert store.usage(session_id) == (30, False)
            assert len(store.load_messages(session_id)) == 4
        with SessionStore(tmp_path / "sessions") as store:
            conversation = build_conversation(
                store, LLMClient(config, http), "rules", create_tool_registry(workspace)
            )
            conversation.resume(session_id)
            assert len(bodies) == 2
            assert conversation.send("continue") == "value is 42"
            assert store.usage(session_id) == (45, False)


def test_toml_selects_messages_and_sends_one_nonstream_request(tmp_path):
    config_path = tmp_path / "llm.toml"
    config_path.write_text(
        '[llm]\nprotocol="messages"\nstream=false\n'
        'base_url="https://test.invalid/anthropic"\n'
        'model="test"\napi_key="test-only"\n',
        encoding="utf-8",
    )
    config = load_llm_config(config_path)
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/anthropic/v1/messages"
        assert body["thinking"] == {"type": "disabled"}
        assert body["system"] == "rules" and body["stream"] is False
        return httpx.Response(
            200,
            json={
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "answer"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = LLMClient(config, http)
        assert client.list_models() == {"test": None}
        result = client.complete(
            [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}]
        )
    assert result.message["content"] == "answer" and result.usage == (5, 1)
    assert len(requests) == 1 and result.input_prefix is not None


def test_messages_usage_includes_cached_and_created_inputs():
    assert parse_messages_usage(
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 3,
            "output_tokens": 4,
        }
    ) == (33, 4)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"input_tokens": 10, "output_tokens": 4},
        {
            "input_tokens": True,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 4,
        },
    ],
)
def test_messages_missing_or_invalid_usage_is_unknown(bad):
    assert parse_messages_usage(bad) is None


def test_two_tool_results_keep_ids_and_share_following_user_message():
    response = {
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": name, "name": "read_file", "input": {"path": name}}
            for name in ("first", "second")
        ],
    }
    assistant = decode_message(response)
    history = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "read"},
        assistant,
        {"role": "tool", "tool_call_id": "first", "content": "one"},
        {"role": "tool", "tool_call_id": "second", "content": "two"},
    ]
    body = encode_messages(history)
    assert body["system"] == "rules"
    assert body["messages"][1]["content"] == response["content"]
    assert [item["tool_use_id"] for item in body["messages"][2]["content"]] == ["first", "second"]
    assert decode_message(
        {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        }
    ) == {"role": "assistant", "content": "done"}


def test_private_reasoning_and_incomplete_reply_are_not_silently_dropped():
    with pytest.raises(ValueError, match="扩展"):
        encode_messages(
            [{"role": "assistant", "content": "answer", "reasoning_content": "private"}]
        )
    with pytest.raises(ValueError, match="完整结束"):
        decode_message(
            {
                "role": "assistant",
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "partial"}],
            }
        )

"""协议切换与失败恢复：真实会话和工具记录，HTTP边界注入取消或超时。"""

import json

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import BudgetError, LLMError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_tool_loop import answer
from tests.support import build_conversation


def conversation(store, http, root, protocol):
    config = LLMConfig("https://test.invalid", "test", "test-only", stream=False, protocol=protocol)
    return build_conversation(store, LLMClient(config, http), "rules", create_tool_registry(root))


def test_completed_tool_history_switches_after_restart_without_reexecuting(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("original evidence", encoding="utf-8")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path == "/chat/completions":
            tool = next(message for message in body["messages"] if message["role"] == "tool")
            assert "original evidence" in tool["content"]
            return httpx.Response(200, json=answer("continued in chat"))
        if len(requests) == 1:
            content = [
                {
                    "type": "tool_use",
                    "id": "read1",
                    "name": "read_file",
                    "input": {"path": "sample.txt"},
                }
            ]
            reason = "tool_use"
        else:
            content, reason = [{"type": "text", "text": "saved"}], "end_turn"
        return httpx.Response(
            200,
            json={
                "role": "assistant",
                "content": content,
                "stop_reason": reason,
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with SessionStore(tmp_path / "sessions") as store:
            current = conversation(store, http, tmp_path, "messages")
            assert current.send("read sample") == "saved"
            session_id = current.session.session_id
        source.unlink()
        with SessionStore(tmp_path / "sessions") as store:
            current = conversation(store, http, tmp_path, "chat_completions")
            current.resume(session_id)
            assert len(requests) == 2
            assert current.send("continue") == "continued in chat"
            events = store.load_events(session_id)
            assert sum(event["type"] == "tool_started" for event in events) == 1
    assert len(requests) == 3


@pytest.mark.parametrize("fault", ["cancel", "timeout"])
def test_unknown_request_usage_survives_restart_and_blocks_switch_retry(tmp_path, fault):
    requests = []

    def fail(request):
        requests.append(request)
        if fault == "cancel":
            raise KeyboardInterrupt()
        raise httpx.ReadTimeout("test interrupted response", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        with SessionStore(tmp_path / "sessions") as store:
            current = conversation(store, http, tmp_path, "messages")
            with pytest.raises(KeyboardInterrupt if fault == "cancel" else LLMError):
                current.send("interrupted request")
            session_id = current.session.session_id
            assert store.usage(session_id) == (0, True)
            assert store.load_messages(session_id) == []
        with SessionStore(tmp_path / "sessions") as store:
            current = conversation(store, http, tmp_path, "chat_completions")
            current.resume(session_id)
            with pytest.raises(BudgetError):
                current.send("do not resend")
            assert store.usage(session_id) == (0, True)
    assert len(requests) == 1

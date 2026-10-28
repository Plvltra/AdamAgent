"""V3.1：上下文预览与单次输入拦截；使用假 HTTP，历史和工具读取实际执行。"""

import json
import sys

import httpx
import pytest

import adam.cli as cli
from adam.config import AgentConfig, ContextConfig, LLMConfig
from adam.context import ContextBuilder
from adam.conversation import Conversation
from adam.errors import BudgetError
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.tool_calls import ToolRegistry


def completion(*, tool=None):
    message = {"role": "assistant", "content": "answer", "reasoning_content": "kept reasoning"}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "inspect first",
            "tool_calls": [
                {
                    "id": "read1",
                    "type": "function",
                    "function": {"name": tool, "arguments": '{"path":"large.txt"}'},
                }
            ],
        }
    return {
        "choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_conversation(store, http, root, *, config=None, prompt="system"):
    return Conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "model-a", "private-api-key"), http),
        prompt,
        ToolRegistry(root),
        AgentConfig(max_output_tokens=1234),
        context_config=config,
    )


def test_context_commands_are_read_only_and_full_is_opt_in(tmp_path):
    requests, shown = [], []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": []})

    inputs = iter(["/context", "/context full", "/context invalid", "/exit"])
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        cli.chat(
            LLMClient(LLMConfig("https://example.test", "model-a", "private-api-key"), http),
            "system-only",
            store,
            read=lambda _: next(inputs),
            write=shown.append,
            workspace=tmp_path,
        )
        assert store.list_sessions() == []
    assert len(requests) == 1  # 仅启动时模型目录查询，三个命令都没有发请求。
    previews = [text for text in shown if text.startswith("上下文>")]
    assert len(previews) == 2 and all("尚未包含下一条用户输入" in text for text in previews)
    full = [json.loads(text) for text in shown if text.startswith("{")]
    assert len(full) == 1
    assert full[0]["messages"] == [{"role": "system", "content": "system-only"}]
    assert full[0]["tools"]
    assert any("用法：/context [full]" == text for text in shown)
    assert "private-api-key" not in "\n".join(shown)
    assert not list((tmp_path / "sessions").rglob("*.jsonl"))


def test_preview_preserves_tool_history_and_tracks_model_session_and_restart(tmp_path):
    (tmp_path / "large.txt").write_text("evidence", encoding="utf-8")
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json=completion(tool="read_file" if len(payloads) == 1 else None)
        )

    root = tmp_path / "sessions"
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(root) as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        session_id = conversation.new("A").session_id
        initial = conversation.preview_context()
        assert conversation.send("inspect") == "answer"
        assert payloads[0]["messages"] == initial.messages + [
            {"role": "user", "content": "inspect"}
        ]
        assert payloads[0]["tools"] == initial.tools
        assert payloads[0]["max_tokens"] == initial.max_output_tokens == 1234
        assert payloads[1]["messages"][-2]["reasoning_content"] == "inspect first"
        assert payloads[1]["messages"][-1]["tool_call_id"] == "read1"
        before = {p: p.read_bytes() for p in root.rglob("*.jsonl")}
        shown = []
        cli.handle_command("/model model-b", conversation, None, shown.append)
        cli.handle_command("/context full", conversation, None, shown.append)
        preview = conversation.preview_context()
        assert preview.model == "model-b"
        assert preview.messages == payloads[1]["messages"] + [completion()["choices"][0]["message"]]
        assert preview.estimate.history_tokens > 0 and preview.estimate.turn_tokens == 0
        assert {p: p.read_bytes() for p in root.rglob("*.jsonl")} == before
        assert store.usage(session_id) == (30, False)  # 本地估算不进入实际消费记录。
        conversation.new("B")
        assert conversation.preview_context().messages == initial.messages
        conversation.resume(session_id)
        assert conversation.preview_context().messages == preview.messages
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("preview made a request"))
        ) as http,
        SessionStore(root) as store,
    ):
        conversation = make_conversation(store, http, tmp_path, prompt="new default")
        conversation.resume(session_id)
        assert conversation.preview_context().messages == preview.messages


@pytest.mark.parametrize("source", ["tools", "input"])
def test_input_budget_blocks_without_request_or_unknown_usage(tmp_path, source):
    requests = []
    config = ContextConfig(
        max_input_tokens=64 if source == "tools" else 8192, safety_margin_tokens=0
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: requests.append(request))
        ) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path, config=config)
        session_id = conversation.new("limit").session_id
        history = store.load_messages(session_id)
        before = conversation.preview_context()
        text = "中" * 12000 if source == "input" else "small"
        with pytest.raises(BudgetError) as caught:
            conversation.send(text)
        assert caught.value.code == "context_input_limit"
        assert "超出" in str(caught.value) and "未发送本次请求" in str(caught.value)
        events = store.load_events(session_id)
        assert not any(event["type"] == "request_started" for event in events)
        assert events[-1]["payload"]["status"] == "failed"
        assert events[-1]["payload"]["reason"] == "context_input_limit"
        assert events[-2]["payload"]["user_text"] == text
        assert store.load_messages(session_id) == history
        assert store.usage(session_id) == (0, False)
        assert conversation.preview_context() == before
        shown = []
        cli.handle_command("/context", conversation, None, shown.append)
        assert any(text.startswith("上下文>") for text in shown)  # 超限仍能诊断。
    assert requests == []


def test_tool_result_growth_is_checked_before_next_model_request(tmp_path):
    (tmp_path / "large.txt").write_text("中" * 6000, encoding="utf-8")
    requests = []

    def handle(request):
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(200, json=completion(tool="read_file"))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        initial = conversation.preview_context().estimate.input_tokens
        # 给首条短输入留空间；真实文件工具返回后必然超出这一预算。
        conversation.context_builder = ContextBuilder(
            ContextConfig(max_input_tokens=initial + 300, safety_margin_tokens=0),
            1234,
        )
        with pytest.raises(BudgetError) as caught:
            conversation.send("read")
        assert caught.value.code == "context_input_limit"
        session_id = conversation.session.session_id
        events = store.load_events(session_id)
        assert len([e for e in events if e["type"] == "request_started"]) == 1
        assert any(e["type"] == "tool_finished" for e in events)
        request = next(e["payload"] for e in events if e["type"] == "request_finished")
        assert request["status"] == "succeeded" and request["reason"] == "completed"
        assert events[-1]["payload"]["reason"] == "context_input_limit"
        assert store.usage(session_id) == (15, False)
        assert store.load_messages(session_id) == []
    assert len(requests) == 1


def test_main_loads_context_limits_and_cli_shows_local_rejection(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text("[context]\nmax_input_tokens=1\nsafety_margin_tokens=0\n", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="private-api-key"\n',
        encoding="utf-8",
    )
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": []})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    original_chat = cli.chat
    inputs = iter(["/context", "hello", "/exit"])

    def chat_with_input(*args, **kwargs):
        return original_chat(*args, **kwargs, read=lambda _: next(inputs))

    monkeypatch.setattr(cli, "chat", chat_with_input)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: http)
    monkeypatch.setattr(
        sys, "argv", ["adam", "--config", str(config), "--workspace", str(tmp_path)]
    )
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "上限 1 token" in output and "执行受限" in output
    assert "context" in output and "private-api-key" not in output
    assert len(requests) == 1
    with SessionStore(tmp_path / "sessions") as store:
        session_id = store.list_sessions()[0].session_id
        assert store.load_events(session_id)[-1]["payload"]["reason"] == "context_input_limit"
        assert store.usage(session_id) == (0, False)

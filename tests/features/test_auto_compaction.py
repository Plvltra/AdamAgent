"""自动压缩的触发、预算和停止边界；真实会话/工具，远端响应使用假 HTTP。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config import AgentConfig, ContextConfig, LLMConfig
from adam.diagnostics import read_report
from adam.errors import AgentError, BudgetError
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.tool_calls import ToolLimits
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


@pytest.fixture
def auto_chat(tmp_path):
    requests, shown = [], []
    control = {"chat_tool": False, "fail_summary": False}

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        summarizing = "tools" not in body
        text = "目标：项目代号 ORION，保留这个约束。" if summarizing else "done"
        if summarizing and control["fail_summary"]:
            text = " "
        message = {"role": "assistant", "content": text}
        tool = control["chat_tool"] and not summarizing
        if tool:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"large.txt"}'},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "tool_calls" if tool else "stop", "message": message}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    (tmp_path / "large.txt").write_text(("中" * 100 + "\n") * 100, encoding="utf-8")
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key"), http),
            "system",
            create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=24000)),
            notify=shown.append,
            context_config=ContextConfig(max_input_tokens=9000, safety_margin_tokens=0),
        )
        session_id = conversation.new().session_id
        for index in range(8):
            store.append_turn(session_id, f"step {index} ORION", "中" * 1000)
        yield conversation, requests, shown, control


def test_automatic_summary_precedes_chat_and_uses_the_shared_budget(auto_chat):
    conversation, requests, shown, _ = auto_chat
    before = conversation.preview_context()
    assert before.candidate_input_tokens >= 7200 and requests == []
    assert conversation.send("next") == "done"
    assert len(requests) == 2 and "tools" not in requests[0] and "tools" in requests[1]
    summary = conversation.store.load_summary(conversation.session.session_id)
    assert summary is not None
    assert any(
        message["content"] == summary.message()["content"] for message in requests[1]["messages"]
    )
    assert sum("自动更新" in text for text in shown) == 1
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    assert report["known_tokens"] == 60
    assert report["compactions"][0]["turn_id"] == report["turns"][-1]["turn_id"]
    assert len(report["turns"][-1]["requests"]) == 1


@pytest.mark.parametrize("limit,expected_calls", [(1, 0), (2, 2)])
def test_summary_cannot_escape_model_request_limit(auto_chat, limit, expected_calls):
    conversation, requests, _, control = auto_chat
    conversation.limits = AgentConfig(max_model_requests=limit)
    control["chat_tool"] = True
    with pytest.raises(BudgetError) as caught:
        conversation.send("read")
    assert caught.value.code == "request_limit" and len(requests) == expected_calls
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    assert report["turns"][-1]["tools"] == []  # 不执行没有后续回答请求额度的工具。


def test_large_current_tool_result_stops_without_a_second_summary(auto_chat):
    conversation, requests, _, control = auto_chat
    control["chat_tool"] = True
    with pytest.raises(BudgetError) as caught:
        conversation.send("read")
    assert caught.value.code == "context_input_limit"
    assert len(requests) == 2
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    assert len(report["compactions"]) == 1 and report["compactions"][0]["status"] == "succeeded"
    assert len(report["turns"][-1]["tools"]) == 1
    assert report["turns"][-1]["status"] == "failed"
    assert len(conversation.store.load_turns(conversation.session.session_id)) == 8


def test_summary_failure_stops_without_chat_or_retry(auto_chat):
    conversation, requests, _, control = auto_chat
    control["fail_summary"] = True
    with pytest.raises(AgentError):
        conversation.send("next")
    assert len(requests) == 1
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    assert report["compactions"][0]["status"] == report["turns"][-1]["status"] == "failed"
    assert report["known_tokens"] == 30 and not report["unknown_usage"]
    assert conversation.store.load_summary(conversation.session.session_id) is None


def test_known_session_limit_is_rechecked_after_summary(auto_chat):
    conversation, requests, _, _ = auto_chat
    conversation.limits = AgentConfig(max_session_tokens=30)
    with pytest.raises(BudgetError) as caught:
        conversation.send("next")
    assert caught.value.code == "session_token_limit" and len(requests) == 1
    assert conversation.store.load_summary(conversation.session.session_id) is not None


def test_auto_off_and_low_pressure_do_not_summarize(auto_chat):
    conversation, requests, shown, _ = auto_chat
    conversation.context_builder.limits = replace(
        conversation.context_builder.limits, auto_compact=False
    )
    conversation.send("next")
    assert len(requests) == 1 and "tools" in requests[0]
    conversation.context_builder.limits = ContextConfig()
    conversation.new("short")
    conversation.send("hello")
    assert len(requests) == 2 and "tools" in requests[1]
    assert not any("自动更新" in text for text in shown)


def test_irreducible_current_input_does_not_spend_on_summary(auto_chat):
    conversation, requests, _, _ = auto_chat
    with pytest.raises(BudgetError) as caught:
        conversation.send("中" * 15000)
    assert caught.value.code == "context_input_limit" and requests == []
    assert conversation.store.load_summary(conversation.session.session_id) is None


def test_auto_summary_uses_pruned_material_and_records_its_source(auto_chat):
    """构造已成功的工具轨迹，核对摘要请求确实使用缩减副本而非重新塞入原大结果。"""
    conversation, requests, _, _ = auto_chat
    session_id = conversation.new("tool-heavy").session_id
    store = conversation.store
    turn = store.start_turn(session_id, "read")
    request = store.start_request(session_id, turn, "test")
    store.finish_request(session_id, turn, request, (1, 1))
    link = {"turn_id": turn, "request_id": request, "tool_call_id": "old-read"}
    output = json.dumps({"ok": True, "text": "中" * 6000}, ensure_ascii=False)
    store.append_tool_event(
        session_id,
        "tool_started",
        {
            **link,
            "name": "read_file",
            "arguments": '{"path":"old.txt"}',
        },
    )
    store.append_tool_event(session_id, "tool_finished", {**link, "output": output})
    store.append_messages(
        session_id,
        turn,
        [
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "old-read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"old.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "old-read", "content": output},
            {"role": "assistant", "content": "done"},
        ],
    )
    for _ in range(3):
        store.append_turn(session_id, "noise", "中" * 1000)
    for _ in range(2):
        store.append_turn(session_id, "recent", "short")
    conversation.context_builder.limits = ContextConfig(
        max_input_tokens=6000, safety_margin_tokens=0
    )
    conversation.send("next")
    assert len(requests) == 2 and "tools" not in requests[0]
    material = json.loads(requests[0]["messages"][1]["content"])
    tool = next(m for item in material["turns"] for m in item["messages"] if m["role"] == "tool")
    assert json.loads(tool["content"])["context_pruned"]
    report = read_report(
        next(path for path in store.root.rglob("*.jsonl") if path.stem == session_id)
    )
    assert report["compactions"][0]["pruned_tools"][0]["tool_call_id"] == "old-read"
    assert any(message.raw.get("content") == output for message in store.load_messages(session_id))

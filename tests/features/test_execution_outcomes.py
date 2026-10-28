"""结束原因与耗时：实际业务经过假 HTTP、文件与日志，时钟只在本模块控制。"""

import json

import httpx
import pytest

import adam.conversation as conversation_module
from adam.config import LLMConfig
from adam.conversation import Conversation
from adam.llm import LLMClient, LLMError
from adam.storage import SessionStore
from adam.tool_calls import ToolRegistry


@pytest.fixture
def clock(monkeypatch):
    """只替换业务计时入口，HTTP 和文件库的内部时钟保持正常。"""
    current = [9_000_000_000]
    monkeypatch.setattr(conversation_module, "monotonic_ns", lambda: current[0])

    def advance(milliseconds):
        current[0] += milliseconds * 1_000_000

    return advance


def response(*, tool=False):
    message = {"role": "assistant", "content": "done"}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "file1",
                    "type": "function",
                    "function": {"name": "file_info", "arguments": '{"path":"."}'},
                }
            ],
        }
    return {
        "choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


def make_conversation(store, http, workspace, notify=None):
    return Conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
        "",
        ToolRegistry(workspace),
        notify=notify,
    )


@pytest.mark.parametrize("last_fails", [False, True])
def test_request_and_turn_have_separate_durations_and_reasons(tmp_path, clock, last_fails):
    """两次请求耗时独立；整轮包含工具阶段，成功标记仍跟随完整消息提交。"""
    requests = []

    def handle(request):
        requests.append(request)
        clock(125 if len(requests) == 1 else 250)
        if len(requests) == 2 and last_fails:
            return httpx.Response(503, text="private-server-detail")
        return httpx.Response(200, json=response(tool=len(requests) == 1))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path, lambda _: clock(25))
        if last_fails:
            with pytest.raises(LLMError):
                conversation.send("inspect")
        else:
            assert conversation.send("inspect") == "done"
        session_id = conversation.session.session_id
    with SessionStore(tmp_path / "s") as store:
        events = store.load_events(session_id)
        ends = [e["payload"] for e in events if e["type"] == "request_finished"]
        assert [e["duration_ms"] for e in ends] == [125, 250]
        assert [e["reason"] for e in ends] == [
            "completed",
            "api_http" if last_fails else "completed",
        ]
        turn = events[-1]
        assert turn["type"] == ("turn_finished" if last_fails else "turn_completed")
        assert turn["payload"]["duration_ms"] == 400
        assert turn["payload"]["reason"] == ("api_http" if last_fails else "completed")
        if last_fails:
            assert turn["payload"]["error"] == ends[-1]["error"]
            assert turn["payload"]["error"]["http_status"] == 503
            assert store.usage(session_id) == (12, True)
            assert store.load_messages(session_id) == []
        else:
            assert turn["payload"]["error"] is None
            assert store.usage(session_id) == (24, False)
            assert store.load_messages(session_id)[-1].content == "done"
        assert "private-server-detail" not in json.dumps(events)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "scenario,reason,failure",
    [
        ("timeout", "api_timeout", LLMError),
        ("network", "api_connection", LLMError),
        ("protocol", "invalid_response", LLMError),
        ("interrupt", "interrupted", KeyboardInterrupt),
        ("exit", "process_exit", SystemExit),
        ("bug", "internal_error", TypeError),
    ],
)
def test_failure_reason_does_not_depend_on_raw_error_text(
    tmp_path, clock, scenario, reason, failure
):
    """不同失败有稳定原因码；原始网络、程序错误和响应正文都不进入日志。"""
    requests = []

    def handle(request):
        requests.append(request)
        clock(37)
        if scenario == "timeout":
            raise httpx.ReadTimeout("private-detail", request=request)
        if scenario == "network":
            raise httpx.ConnectError("private-detail", request=request)
        if scenario == "protocol":
            return httpx.Response(200, text="private-detail")
        raise failure("private-detail")

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        with pytest.raises(failure):
            conversation.send("question")
        events = store.load_events(conversation.session.session_id)
        request = next(e["payload"] for e in events if e["type"] == "request_finished")
        turn = events[-1]["payload"]
        assert request["reason"] == turn["reason"] == reason
        assert request["duration_ms"] == turn["duration_ms"] == 37
        assert request["error"]["message"] and request["error"]["http_status"] is None
        assert "private-detail" not in json.dumps(events)
        assert store.usage(conversation.session.session_id) == (0, True)
    assert len(requests) == 1

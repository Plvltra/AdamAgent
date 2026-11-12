"""从真实 JSONL 验收结束原因与耗时；HTTP 和时钟可控，不调用付费服务。"""

import json

import httpx
import pytest

import adam.conversation as conversation_module
from adam.config import AgentConfig, LLMConfig
from adam.conversation import Conversation
from adam.errors import BudgetError
from adam.llm import LLMClient, LLMError
from adam.persistence import SessionStore
from adam.persistence.session_log import parse_log
from adam.tool_calls import ToolRegistry


@pytest.fixture
def clock(monkeypatch):
    """只替换对话计时，避免实际休眠和影响 HTTP/文件锁的内部时钟。"""
    value = [0]
    monkeypatch.setattr(conversation_module, "monotonic_ns", lambda: value[0])
    return value


def response(tool=False, usage=True):
    message = {"role": "assistant", "content": "done"}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"sample.txt"}',
                    },
                }
            ],
        }
    data = {"choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}]}
    if usage:
        data["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
    return data


def conversation(store, http, workspace, **kwargs):
    return Conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
        "",
        ToolRegistry(workspace),
        **kwargs,
    )


def read_log(root, session_id):
    return parse_log(next(root.glob(f"*/*/*/{session_id}.jsonl")).read_bytes(), session_id)


@pytest.mark.parametrize("http_failure", [False, True])
def test_two_requests_keep_separate_outcomes(tmp_path, monkeypatch, clock, http_failure):
    """真实文件读取发生在两次请求之间；第二次失败不改写第一请求的成功。"""
    (tmp_path / "sample.txt").write_text("evidence", encoding="utf-8")
    calls = []

    def handle(request):
        calls.append(request)
        clock[0] += (125 if len(calls) == 1 else 250) * 1_000_000
        if len(calls) == 1:
            return httpx.Response(200, json=response(tool=True))
        if http_failure:
            return httpx.Response(503, text="private-server-secret")
        return httpx.Response(200, json=response())

    root = tmp_path / "sessions"
    with SessionStore(root) as store, httpx.Client(transport=httpx.MockTransport(handle)) as http:
        chat = conversation(store, http, tmp_path)
        execute = chat.tool_registry.execute

        def timed_tool(*args, **kwargs):
            clock[0] += 25_000_000
            return execute(*args, **kwargs)

        monkeypatch.setattr(chat.tool_registry, "execute", timed_tool)
        if http_failure:
            with pytest.raises(LLMError, match="503"):
                chat.send("读取 sample.txt 并解释")
        else:
            assert chat.send("读取 sample.txt 并解释") == "done"
        session_id = chat.session.session_id

    with SessionStore(root) as store:
        log = read_log(root, session_id)
        turn_id, turn = next(iter(log.turns.items()))
        requests = list(log.requests.values())
        assert len(requests) == 2 and all(r["turn_id"] == turn_id for r in requests)
        assert [r["duration_ms"] for r in requests] == [125, 250]
        assert [r["reason"] for r in requests] == [
            "completed",
            "api_http_error" if http_failure else "completed",
        ]
        assert turn["user_text"] == "读取 sample.txt 并解释"
        assert turn["duration_ms"] == 400
        assert turn["reason"] == requests[-1]["reason"]
        assert turn["status"] == ("failed" if http_failure else "succeeded")
        assert requests[-1]["http_status"] == (503 if http_failure else None)
        assert turn["http_status"] == (503 if http_failure else None)
        assert requests[0]["error_message"] is None
        assert store.usage(session_id) == ((12, True) if http_failure else (24, False))
        assert len(store.load_messages(session_id)) == (0 if http_failure else 4)
        assert "private-server-secret" not in json.dumps(log.events)
        tool = next(iter(log.tools.values()))
        assert tool["request_id"] == next(iter(log.requests))
        assert tool["finished"]
        if http_failure:
            assert "503" in requests[-1]["error_message"]
        else:
            completed = next(e for e in log.events if e["type"] == "turn_completed")
            assert completed["payload"]["reason"] == "completed"


def test_timer_boundaries_exclude_final_writes(tmp_path, monkeypatch, clock):
    """人为增加各写入边界耗时，验证请求不含结算、整轮不含最终提交。"""

    def handle(request):
        clock[0] += 125_000_000
        return httpx.Response(200, json=response())

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        for name, milliseconds in [
            ("start_turn", 100),
            ("start_request", 10),
            ("finish_request", 20),
            ("commit_turn", 30),
        ]:
            original = getattr(store, name)

            def delayed(*args, original=original, ms=milliseconds, **kwargs):
                result = original(*args, **kwargs)
                clock[0] += ms * 1_000_000
                return result

            monkeypatch.setattr(store, name, delayed)
        chat = conversation(store, http, tmp_path)
        chat.send("hello")
        log = read_log(store.root, chat.session.session_id)
        assert next(iter(log.requests.values()))["duration_ms"] == 125
        assert next(iter(log.turns.values()))["duration_ms"] == 155


@pytest.mark.parametrize(
    "error, reason, status",
    [
        (httpx.ReadTimeout("private-secret"), "api_timeout", "failed"),
        (httpx.ConnectError("private-secret"), "api_connection", "failed"),
        (TypeError("private-secret"), "internal_error", "failed"),
        (KeyboardInterrupt("private-secret"), "interrupted", "interrupted"),
        (SystemExit("private-secret"), "process_exit", "interrupted"),
    ],
)
def test_exception_reason_is_persisted_without_private_details(
    tmp_path, clock, error, reason, status
):
    def handle(request):
        clock[0] += 7_000_000
        raise error

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        chat = conversation(store, http, tmp_path)
        expected = LLMError if isinstance(error, httpx.RequestError) else type(error)
        with pytest.raises(expected):
            chat.send("hello")
        log = read_log(store.root, chat.session.session_id)
        for item in [*log.requests.values(), *log.turns.values()]:
            assert (item["status"], item["reason"], item["duration_ms"]) == (status, reason, 7)
            assert item["http_status"] is None
        assert "private-secret" not in json.dumps(log.events)


@pytest.mark.parametrize(
    "case, reason",
    [
        ("request", "request_limit"),
        ("tool", "tool_limit"),
        ("session", "session_limit"),
    ],
)
def test_budget_reason_does_not_change_successful_request(tmp_path, clock, case, reason):
    limits = (
        AgentConfig(max_model_requests=1)
        if case == "request"
        else (
            AgentConfig(max_tool_calls=0) if case == "tool" else AgentConfig(max_session_tokens=1)
        )
    )
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(tool=True)))
        ) as http,
    ):
        chat = conversation(store, http, tmp_path, limits=limits)
        with pytest.raises(BudgetError):
            chat.send("read")
        log = read_log(store.root, chat.session.session_id)
        assert next(iter(log.requests.values()))["reason"] == "completed"
        assert next(iter(log.turns.values()))["reason"] == reason
        assert not log.tools
        assert not store.load_messages(chat.session.session_id)


@pytest.mark.parametrize("invalid", [False, True])
def test_unknown_usage_is_distinct_from_invalid_response(tmp_path, clock, invalid):
    data = {"choices": []} if invalid else response(usage=False)
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
        ) as http,
    ):
        chat = conversation(store, http, tmp_path)
        with pytest.raises(LLMError):
            chat.send("hello")
        log = read_log(store.root, chat.session.session_id)
        assert log.unknown_usage
        assert next(iter(log.requests.values()))["reason"] == (
            "invalid_response" if invalid else "completed"
        )
        assert next(iter(log.turns.values()))["reason"] == (
            "invalid_response" if invalid else "usage_unknown"
        )

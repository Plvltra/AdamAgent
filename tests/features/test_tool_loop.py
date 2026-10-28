"""工具循环业务：模型选择工具、反馈错误、预算限制与恢复历史。"""

import json

import httpx
import pytest

from adam.config import AgentConfig, LLMConfig
from adam.conversation import Conversation
from adam.llm import LLMClient, LLMError
from adam.storage import SessionStore, StorageError
from adam.tool_calls import ToolRegistry


def answer(text="done", tokens=10):
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": tokens, "completion_tokens": 2},
    }


def tool_response(calls, tokens=10):
    result = answer(tokens=tokens)
    result["choices"][0] = {
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "provider field",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for call_id, name, arguments in calls
            ],
        },
    }
    return result


def make_conversation(store, http, workspace, limits=None):
    return Conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
        "system",
        ToolRegistry(workspace),
        limits,
    )


def test_search_read_answer_and_restore(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("needle = 42\n", encoding="utf-8")
    payloads = []
    replies = iter(
        [
            tool_response([("search", "search_text", '{"path":".","query":"needle"}')]),
            tool_response(
                [
                    ("read", "read_file", '{"path":"sample.py"}'),
                    ("info", "file_info", '{"path":"sample.py"}'),
                ]
            ),
            answer(),
            answer("continued"),
        ]
    )

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with SessionStore(tmp_path / "sessions") as store:
            conversation = make_conversation(store, http, workspace)
            assert conversation.send("find needle") == "done"
            session_id = conversation.session.session_id
            assert store.usage(session_id) == (36, False)
            assert len(store.load_messages(session_id)) == 7
        with SessionStore(tmp_path / "sessions") as store:
            conversation = make_conversation(store, http, workspace)
            conversation.resume(session_id)
            assert conversation.send("continue") == "continued"
            assert store.usage(session_id) == (48, False)
    assert len(payloads[0]["tools"]) == 9
    assert payloads[0]["max_tokens"] == 4096
    assert payloads[1]["messages"][-2]["reasoning_content"] == "provider field"
    assert payloads[2]["messages"][-2]["tool_call_id"] == "read"
    assert payloads[2]["messages"][-1]["tool_call_id"] == "info"
    assert "needle" in payloads[2]["messages"][-2]["content"]
    assert payloads[3]["messages"][2]["tool_calls"][0]["id"] == "search"


@pytest.mark.parametrize(
    "calls",
    [
        [("a", "unknown", "{}")],
        [("a", "read_file", "{bad")],
        [("a", "read_file", '{"path":1}')],
    ],
)
def test_tool_error_returns_to_model(tmp_path, calls):
    payloads = []
    replies = iter([tool_response(calls), answer()])

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        assert conversation.send("test") == "done"
    assert json.loads(payloads[1]["messages"][-1]["content"])["ok"] is False
    assert payloads[1]["messages"][-1]["tool_call_id"] == "a"


@pytest.mark.parametrize(
    "limits,expected",
    [
        (AgentConfig(max_model_requests=2), 2),
        (AgentConfig(max_tool_calls=1), 2),
        (AgentConfig(max_session_tokens=12), 1),
    ],
)
def test_loop_limits_keep_usage_not_partial_history(tmp_path, limits, expected):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=tool_response([(str(count), "file_info", '{"path":"."}')]))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path, limits)
        with pytest.raises(LLMError):
            conversation.send("loop")
        session_id = conversation.session.session_id
        assert store.load_messages(session_id) == []
        assert store.usage(session_id) == (12 * expected, False)
    assert count == expected


@pytest.mark.parametrize("failure", ["timeout", "missing_usage", "http"])
def test_unknown_usage_blocks_session_after_reopen(tmp_path, failure):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "http":
            return httpx.Response(503)
        result = answer()
        del result["usage"]
        return httpx.Response(200, json=result)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with SessionStore(tmp_path / "s") as store:
            conversation = make_conversation(store, http, tmp_path)
            with pytest.raises(LLMError):
                conversation.send("fail")
            session_id = conversation.session.session_id
        with SessionStore(tmp_path / "s") as store:
            conversation = make_conversation(store, http, tmp_path)
            conversation.resume(session_id)
            with pytest.raises(LLMError, match="不完整"):
                conversation.send("no retry")
            assert store.load_messages(session_id) == []
    assert count == 1


def test_pending_request_after_crash_blocks_budget(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("test", "")
        turn_id = store.start_turn(session_id, "unfinished")
        store.start_request(session_id, turn_id, "test")
    with SessionStore(tmp_path) as store:
        assert store.usage(session_id) == (0, True)


def test_known_usage_after_invalid_response_is_counted(tmp_path):
    result = answer(tokens=30)
    result["choices"][0]["finish_reason"] = "length"
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result))
        ) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        with pytest.raises(LLMError):
            conversation.send("bad response")
        assert store.usage(conversation.session.session_id) == (32, False)
        assert store.load_messages(conversation.session.session_id) == []


def test_storage_rejects_broken_tool_chain_and_old_format(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("test", "")
        turn_id = store.start_turn(session_id, "hi")
        call = tool_response([("a", "read_file", "{}")])["choices"][0]["message"]
        with pytest.raises(ValueError):
            store.append_messages(
                session_id,
                turn_id,
                [
                    {"role": "user", "content": "hi"},
                    call,
                    {"role": "tool", "tool_call_id": "wrong", "content": "x"},
                    {"role": "assistant", "content": "done"},
                ],
            )
        assert store.load_messages(session_id) == []
        path = next(tmp_path.glob("*/*/*/*.jsonl"))
        value = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        value["payload"]["version"] = 2
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with pytest.raises(StorageError, match="旧版"):
            store.get_session(session_id)


def test_session_budget_persists_and_final_answer_can_cross_threshold(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=answer(tokens=15))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with SessionStore(tmp_path / "s") as store:
            conversation = make_conversation(
                store, http, tmp_path, AgentConfig(max_session_tokens=10)
            )
            assert conversation.send("finish") == "done"
            session_id = conversation.session.session_id
        with SessionStore(tmp_path / "s") as store:
            conversation = make_conversation(
                store, http, tmp_path, AgentConfig(max_session_tokens=10)
            )
            conversation.resume(session_id)
            with pytest.raises(LLMError, match="额度"):
                conversation.send("blocked")
            assert store.usage(session_id) == (17, False)
    assert count == 1


def test_duplicate_tool_ids_across_steps_stop_cleanly(tmp_path):
    response = tool_response([("same", "file_info", '{"path":"."}')])
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        SessionStore(tmp_path / "s") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        with pytest.raises(LLMError, match="重复"):
            conversation.send("loop")
        assert store.usage(conversation.session.session_id) == (24, False)
        assert store.load_messages(conversation.session.session_id) == []


def test_directory_error_can_be_corrected_before_reading_file(tmp_path):
    """假模型先访问错误目录，再根据错误反馈列目录并读取正确文件。"""
    (tmp_path / "sample.txt").write_text("correct evidence", encoding="utf-8")
    payloads = []
    replies = iter(
        [
            tool_response([("bad", "list_directory", '{"path":"missing"}')]),
            tool_response([("list", "list_directory", '{"path":"."}')]),
            tool_response([("read", "read_file", '{"path":"sample.txt"}')]),
            answer("located"),
        ]
    )

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        assert conversation.send("find evidence") == "located"
        assert store.load_messages(conversation.session.session_id)[-1].content == "located"
    assert json.loads(payloads[1]["messages"][-1]["content"])["ok"] is False
    assert "sample.txt" in json.loads(payloads[2]["messages"][-1]["content"])["entries"]
    assert "correct evidence" in payloads[3]["messages"][-1]["content"]


def test_tool_batch_over_budget_executes_none(tmp_path, monkeypatch):
    """模型一次要求两个工具但只剩一次额度时，整批都不能执行。"""
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=tool_response(
                [("first", "file_info", '{"path":"."}'), ("second", "file_info", '{"path":"."}')]
            ),
        )

    def forbidden(*args, **kwargs):
        pytest.fail("An over-budget tool batch must execute nothing")

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path, AgentConfig(max_tool_calls=1))
        monkeypatch.setattr(conversation.tool_registry, "execute", forbidden)
        with pytest.raises(LLMError):
            conversation.send("inspect twice")
        session_id = conversation.session.session_id
        assert not any(e["type"] == "tool_started" for e in store.load_events(session_id))
        assert store.load_messages(session_id) == []
        assert store.usage(session_id) == (12, False)
    assert len(requests) == 1


def test_tool_call_id_can_repeat_in_different_successful_turns(tmp_path):
    """同一会话不同轮次可以复用模型工具 ID，恢复时仍按轮次准确关联。"""
    root = tmp_path / "sessions"
    replies = iter(
        [
            tool_response([("same", "file_info", '{"path":"."}')]),
            answer("first answer"),
            tool_response([("same", "file_info", '{"path":"."}')]),
            answer("second answer"),
        ]
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(replies)))
        ) as http,
        SessionStore(root) as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        assert conversation.send("first") == "first answer"
        assert conversation.send("second") == "second answer"
        session_id = conversation.session.session_id
    with SessionStore(root) as store:
        starts = [
            e["payload"] for e in store.load_events(session_id) if e["type"] == "tool_started"
        ]
        assert [s["tool_call_id"] for s in starts] == ["same", "same"]
        assert len({s["turn_id"] for s in starts}) == 2
        assert len(store.load_messages(session_id)) == 8
        assert store.load_messages(session_id)[-1].content == "second answer"

"""验证失败轮次仍可追溯请求和工具，诊断记录不污染成功历史。"""

import json

import httpx
import pytest

from adam.config import LLMConfig
from adam.conversation import Conversation
from adam.llm import LLMClient, LLMError
from adam.storage import SessionStore, StorageError
from adam.tool_calls import ToolRegistry
from adam.tool_calls.command_tools import CommandLogError


def read_events(root, session_id):
    path = next(root.rglob(f"{session_id}.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def response(*, tool=False, text="done", finish_reason="stop"):
    message = {"role": "assistant", "content": text}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sample.txt"}'},
                }
            ],
        }
        finish_reason = "tool_calls"
    return {
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


def make_conversation(store, http, workspace):
    return Conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
        "",
        ToolRegistry(workspace),
    )


@pytest.mark.parametrize("last_request_fails", [True, False])
def test_read_tool_trace_survives_and_success_uses_original_turn(tmp_path, last_request_fails):
    """两个模型请求中间的真实读取独立保存，不依赖最终回答成功。"""
    (tmp_path / "sample.txt").write_text("evidence", encoding="utf-8")
    root = tmp_path / "sessions"
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(200, json=response(tool=True))
        if last_request_fails:
            return httpx.Response(503)
        return httpx.Response(200, json=response())

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(root) as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        if last_request_fails:
            with pytest.raises(LLMError):
                conversation.send("read my file")
        else:
            assert conversation.send("read my file") == "done"
        session_id = conversation.session.session_id

    with SessionStore(root) as store:
        events = read_events(root, session_id)
        started = [e["payload"] for e in events if e["type"] == "turn_started"]
        assert len(started) == 1 and started[0]["user_text"] == "read my file"
        turn_id = started[0]["turn_id"]
        requests = [e["payload"] for e in events if e["type"] == "request_started"]
        assert len(requests) == 2 and len({r["request_id"] for r in requests}) == 2
        assert all(r["turn_id"] == turn_id for r in requests)
        usages = [e["payload"] for e in events if e["type"] == "request_usage"]
        assert len(usages) == 2 and all(r["turn_id"] == turn_id for r in usages)
        finished = [e["payload"] for e in events if e["type"] == "request_finished"]
        assert [item["status"] for item in finished] == [
            "succeeded",
            "failed" if last_request_fails else "succeeded",
        ]
        assert [item["request_id"] for item in finished] == [r["request_id"] for r in requests]
        assert all(item["turn_id"] == turn_id for item in finished)
        tool_start = next(e["payload"] for e in events if e["type"] == "tool_started")
        tool_end = next(e["payload"] for e in events if e["type"] == "tool_finished")
        assert tool_start["name"] == "read_file"
        assert json.loads(tool_start["arguments"]) == {"path": "sample.txt"}
        for item in (tool_start, tool_end):
            assert item["turn_id"] == turn_id
            assert item["request_id"] == requests[0]["request_id"]
            assert item["tool_call_id"] == "read1"
        assert tool_end["output"] == payloads[1]["messages"][-1]["content"]
        assert "evidence" in tool_end["output"]
        completed = [e for e in events if e["type"] == "turn_completed"]
        if last_request_fails:
            assert completed == [] and store.load_messages(session_id) == []
            assert events[-1]["type"] == "turn_finished"
            assert events[-1]["payload"]["turn_id"] == turn_id
            assert events[-1]["payload"]["status"] == "failed"
            assert store.usage(session_id) == (12, True)
        else:
            assert len(completed) == 1 and completed[0]["payload"]["turn_id"] == turn_id
            saved = [e["payload"] for e in events if e["type"] == "message"]
            assert saved and all(e["turn_id"] == turn_id for e in saved)
            assert store.load_messages(session_id)[-1].content == "done"


def test_failed_input_is_diagnostic_only_in_next_turn(tmp_path):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        result = response(finish_reason="length") if len(payloads) == 1 else response()
        return httpx.Response(200, json=result)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        with pytest.raises(LLMError):
            conversation.send("failed input")
        assert conversation.send("fresh input") == "done"
        session_id = conversation.session.session_id
        events = read_events(tmp_path / "sessions", session_id)
        turns = [e["payload"] for e in events if e["type"] == "turn_started"]
        assert [t["user_text"] for t in turns] == ["failed input", "fresh input"]
        assert turns[0]["turn_id"] != turns[1]["turn_id"]
        assert [m.content for m in store.load_messages(session_id)] == ["fresh input", "done"]
        assert store.usage(session_id) == (24, False)
    assert payloads[1]["messages"] == [{"role": "user", "content": "fresh input"}]


def test_input_record_failure_prevents_model_request(tmp_path, monkeypatch):
    def fail(*args):
        raise StorageError("input record failed")

    monkeypatch.setattr(SessionStore, "start_turn", fail)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("Must not request model"))
        ) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        with pytest.raises(StorageError, match="input record failed"):
            make_conversation(store, http, tmp_path).send("question")


@pytest.mark.parametrize("failed_event", ["tool_started", "tool_finished"])
def test_tool_journal_failure_stops_before_further_actions(tmp_path, monkeypatch, failed_event):
    """开始日志失败不执行工具；结束日志失败不把未落盘结果交给下一次模型请求。"""
    requests = []
    executions = []

    def handle(request):
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(200, json=response(tool=True))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        append = store.append_tool_event

        def append_or_fail(session_id, kind, payload):
            if kind == failed_event:
                raise StorageError("tool record failed")
            return append(session_id, kind, payload)

        def execute(name, arguments, **kwargs):
            executions.append(name)
            return '{"ok":true}'

        monkeypatch.setattr(store, "append_tool_event", append_or_fail)
        monkeypatch.setattr(conversation.tool_registry, "execute", execute)
        with pytest.raises(StorageError, match="tool record failed"):
            conversation.send("read")
        assert len(requests) == 1
        assert executions == ([] if failed_event == "tool_started" else ["read_file"])
        assert store.load_messages(conversation.session.session_id) == []


@pytest.mark.parametrize(
    "invalid_action",
    [
        "unknown_turn",
        "wrong_usage_turn",
        "unknown_request",
        "duplicate_usage",
        "duplicate_tool_id",
        "wrong_tool_request",
        "input_mismatch",
        "duplicate_completion",
        "wrong_command_request",
    ],
)
def test_invalid_associations_are_rejected_before_writing(tmp_path, invalid_action):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("links", "")
        first = store.start_turn(session_id, "first")
        second = store.start_turn(session_id, "second")
        request = store.start_request(session_id, first, "test")
        store.finish_request(session_id, first, request, (1, 2))
        other_request = store.start_request(session_id, first, "test")
        store.finish_request(session_id, first, other_request, (0, 0))
        link = {"turn_id": first, "request_id": request, "tool_call_id": "tool1"}
        store.append_tool_event(
            session_id,
            "tool_started",
            {**link, "name": "run_command", "arguments": '{"argv":["python"]}'},
        )
        plain_messages = [
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "done"},
        ]
        if invalid_action == "duplicate_completion":
            store.append_messages(session_id, second, plain_messages)
        if invalid_action == "wrong_usage_turn":
            request = store.start_request(session_id, first, "test")
        path = next(tmp_path.rglob(f"{session_id}.jsonl"))
        original = path.read_bytes()
        original_usage = store.usage(session_id)
        with pytest.raises(ValueError):
            if invalid_action == "unknown_turn":
                store.start_request(session_id, "missing", "test")
            elif invalid_action == "wrong_usage_turn":
                store.finish_request(session_id, second, request, (1, 2))
            elif invalid_action == "unknown_request":
                store.append_tool_event(
                    session_id,
                    "tool_started",
                    {**link, "request_id": "missing", "name": "file_info", "arguments": "{}"},
                )
            elif invalid_action == "duplicate_usage":
                store.finish_request(session_id, first, request, (1, 2))
            elif invalid_action == "duplicate_tool_id":
                store.append_tool_event(
                    session_id,
                    "tool_started",
                    {**link, "request_id": other_request, "name": "file_info", "arguments": "{}"},
                )
            elif invalid_action == "wrong_tool_request":
                store.append_tool_event(
                    session_id,
                    "tool_finished",
                    {**link, "request_id": other_request, "output": '{"ok":true}'},
                )
            elif invalid_action == "input_mismatch":
                store.append_messages(
                    session_id,
                    second,
                    [{"role": "user", "content": "wrong"}, plain_messages[-1]],
                )
            elif invalid_action == "duplicate_completion":
                store.append_messages(session_id, second, plain_messages)
            else:
                store.append_command_event(
                    session_id,
                    "command_started",
                    {
                        **link,
                        "request_id": other_request,
                        "execution_id": "command1",
                        "argv": ["python"],
                        "cwd": str(tmp_path),
                    },
                )
        assert path.read_bytes() == original
        assert store.usage(session_id) == original_usage
        assert store.get_session(session_id).title == "links"


def test_unfinished_turn_is_readable_and_does_not_imply_unknown_usage(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("unfinished", "")
        first = store.start_turn(session_id, "input only")
    with SessionStore(tmp_path) as store:
        store.select_session(session_id)
        assert store.load_messages(session_id) == []
        assert store.usage(session_id) == (0, False)
        second = store.start_turn(session_id, "new input")
        assert first != second
        store.start_request(session_id, second, "test")
    with SessionStore(tmp_path) as store:
        assert store.usage(session_id) == (0, True)
        assert (
            len([e for e in read_events(tmp_path, session_id) if e["type"] == "turn_started"]) == 2
        )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_old_session_is_rejected_without_rewriting(tmp_path, version):
    with SessionStore(tmp_path) as store:
        session_id = store.create_session("old", "")
        path = next(tmp_path.rglob(f"{session_id}.jsonl"))
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["payload"]["version"] = version
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        original = path.read_bytes()
        with pytest.raises(StorageError, match="旧版"):
            store.select_session(session_id)
        assert path.read_bytes() == original


def test_command_log_validation_failure_is_fatal(tmp_path, monkeypatch):
    """命令日志校验失败不能伪装成可重试的参数错误。"""
    requests = []
    approvals = []
    reply = response(tool=True)
    reply["choices"][0]["message"]["tool_calls"][0]["function"] = {
        "name": "run_command",
        "arguments": '{"argv":["python","-c","print(42)"]}',
    }

    def handle(request):
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(200, json=reply)

    def fail_record(*args):
        raise ValueError("invalid command association")

    def forbidden(*args, **kwargs):
        pytest.fail("Must not start process when command log validation fails")

    monkeypatch.setattr("adam.tool_calls.command_tools.subprocess.Popen", forbidden)
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = Conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            ToolRegistry(tmp_path, confirm_command=lambda c: approvals.append(c) or True),
        )
        monkeypatch.setattr(store, "append_command_event", fail_record)
        with pytest.raises(CommandLogError):
            conversation.send("run command")
        assert len(approvals) == len(requests) == 1
        events = read_events(tmp_path / "sessions", conversation.session.session_id)
        assert any(e["type"] == "tool_started" for e in events)
        assert not any(e["type"] == "tool_finished" for e in events)
        assert store.load_messages(conversation.session.session_id) == []

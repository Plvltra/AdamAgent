"""命令执行业务：批准、拒绝、超时、真实进程结果与执行记录。"""

import json
import sys

import httpx
import pytest

from adam.cli import chat, confirm_command
from adam.config import LLMConfig
from adam.conversation import Conversation
from adam.llm import LLMClient, LLMError
from adam.storage import SessionStore, StorageError
from adam.tool_calls import ToolLimits, ToolRegistry
from adam.tool_calls.command_tools import CommandLogError, CommandRequest


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


def invoke(registry, argv=None, cwd=".", record=None, **options):
    return json.loads(
        registry.execute(
            "run_command",
            json.dumps(
                {"argv": argv or [sys.executable, "-c", "print(42)"], "cwd": cwd, **options}
            ),
            record_execution=record,
        )
    )


def test_arbitrary_script_and_arguments(project):
    (project / "script.py").write_text("import sys; print(sys.argv[1])", encoding="utf-8")
    approved = []
    registry = ToolRegistry(project, confirm_command=lambda c: approved.append(c) or True)
    result = invoke(registry, [sys.executable, "script.py", "a b; & literal"])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "a b; & literal"
    assert approved[0].argv[-1] == "a b; & literal"


def test_git_and_alternate_cwd(project):
    outside = project.parent / "other"
    outside.mkdir()
    registry = ToolRegistry(project, confirm_command=lambda _: True)
    result = invoke(registry, ["git", "--version"], cwd=str(outside))
    assert result["exit_code"] == 0 and "git version" in result["stdout"]
    result = invoke(
        registry, [sys.executable, "-c", "import os; print(os.getcwd())"], cwd="../other"
    )
    assert result["stdout"].strip() == str(outside)


def test_nonzero_exit_and_stderr(project):
    result = invoke(
        ToolRegistry(project, confirm_command=lambda _: True),
        [sys.executable, "-c", "import sys; print('problem', file=sys.stderr); sys.exit(7)"],
    )
    assert result["ok"] and result["exit_code"] == 7
    assert result["stderr"].strip() == "problem"


def test_denied_and_invalid_requests_do_not_spawn(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not spawn")

    monkeypatch.setattr("adam.tool_calls.command_tools.subprocess.Popen", forbidden)
    registry = ToolRegistry(project)
    assert invoke(registry)["status"] == "denied"
    for arguments in [
        {"argv": []},
        {"argv": [1]},
        {"argv": ["python"], "timeout_seconds": True},
        {"argv": ["python"], "timeout_seconds": 9999},
        {"argv": ["python"], "extra": 1},
        {"argv": ["python"], "cwd": "missing"},
    ]:
        assert not json.loads(registry.execute("run_command", json.dumps(arguments)))["ok"]


def test_timeout_and_bounded_output(project):
    registry = ToolRegistry(project, confirm_command=lambda _: True)
    result = invoke(
        registry, [sys.executable, "-c", "import time; time.sleep(20)"], timeout_seconds=1
    )
    assert result["timed_out"] and result["exit_code"] is not None
    registry = ToolRegistry(
        project, limits=ToolLimits(max_result_chars=512), confirm_command=lambda _: True
    )
    encoded = registry.execute(
        "run_command", json.dumps({"argv": [sys.executable, "-c", "print('x'*20000)"]})
    )
    result = json.loads(encoded)
    assert len(encoded) <= 512 and result["truncated"] and result["exit_code"] == 0


def test_start_failure_is_recorded(project, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private error")

    monkeypatch.setattr("adam.tool_calls.command_tools.subprocess.Popen", fail)
    events = []
    result = invoke(
        ToolRegistry(project, confirm_command=lambda _: True),
        record=lambda kind, data: events.append(kind),
    )
    assert result["status"] == "start_failed" and result["exit_code"] is None
    assert events == ["command_started", "command_finished"]
    assert "private error" not in json.dumps(result)


@pytest.mark.parametrize(
    "response,expected", [("y", True), ("Y", True), ("", False), ("yes", False), ("n", False)]
)
def test_confirmation_is_explicit(project, response, expected):
    shown = []
    command = CommandRequest((sys.executable, "-c", "print(42)"), project, 30)
    assert confirm_command(command, lambda _: response, shown.append) == expected
    assert json.loads(shown[1].split("> ", 1)[1])[0] == sys.executable


def tool_reply():
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "command1",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps(
                                    {"argv": [sys.executable, "-c", "print(42)"]}
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


def final_reply():
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Command completed."},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


@pytest.mark.parametrize("decision", ["y", "n"])
def test_cli_model_tool_loop_and_restore(project, tmp_path, decision):
    payloads = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=tool_reply() if len(payloads) == 1 else final_reply())

    inputs = iter(["run tests", decision, "/exit"])
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        client = LLMClient(LLMConfig("https://example.test", "test", "secret"), http)
        shown = []
        chat(client, "", store, read=lambda _: next(inputs), write=shown.append, workspace=project)
    expected = "completed" if decision == "y" else "denied"
    assert len(payloads) == (2 if decision == "y" else 1)
    if decision == "y":
        assert json.loads(payloads[1]["messages"][-1]["content"])["status"] == expected
    else:
        assert any("本轮已停止" in line for line in shown)
    with SessionStore(tmp_path / "sessions") as store:
        session = store.list_sessions()[0]
        executions = store.load_executions(session.session_id)
        assert len(executions) == 1
        assert executions[0]["status"] == expected
        assert executions[0]["tool_call_id"] == "command1"
        history = store.load_messages(session.session_id)
        if decision == "y":
            assert history[-1].content == "Command completed."
        else:
            assert history == []


def test_execution_survives_later_model_failure(project, tmp_path):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=tool_reply()) if count == 1 else httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = Conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            ToolRegistry(project, confirm_command=lambda _: True),
        )
        with pytest.raises(LLMError):
            conversation.send("run tests")
        session_id = conversation.session.session_id
        assert store.load_messages(session_id) == []
        execution = store.load_executions(session_id)[0]
        assert execution["status"] == "completed"
        path = next((tmp_path / "sessions").rglob(f"{session_id}.jsonl"))
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        started_turn = next(e["payload"] for e in events if e["type"] == "turn_started")
        requests = [e["payload"] for e in events if e["type"] == "request_started"]
        assert len(requests) == 2
        assert execution["turn_id"] == started_turn["turn_id"]
        assert execution["request_id"] == requests[0]["request_id"]
        assert all(r["turn_id"] == started_turn["turn_id"] for r in requests)
        tool_events = [e["payload"] for e in events if e["type"].startswith("tool_")]
        assert len(tool_events) == 2
        assert all(e["tool_call_id"] == execution["tool_call_id"] for e in tool_events)


def test_record_failure_prevents_spawn(project, monkeypatch):
    def fail_record(kind, payload):
        raise OSError("disk full")

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not spawn")

    monkeypatch.setattr("adam.tool_calls.command_tools.subprocess.Popen", forbidden)
    with pytest.raises(CommandLogError):
        invoke(ToolRegistry(project, confirm_command=lambda _: True), record=fail_record)


def test_unknown_execution_blocks_replay_after_restart(project, tmp_path):
    root = tmp_path / "sessions"
    with SessionStore(root) as store:
        session_id = store.create_session("test", "")
        turn_id = store.start_turn(session_id, "run command")
        request_id = store.start_request(session_id, turn_id, "test")
        store.finish_request(session_id, turn_id, request_id, (0, 0))
        link = {"turn_id": turn_id, "request_id": request_id, "tool_call_id": "t"}
        store.append_tool_event(
            session_id,
            "tool_started",
            {**link, "name": "run_command", "arguments": '{"argv":["python"]}'},
        )
        store.append_command_event(
            session_id,
            "command_started",
            {**link, "execution_id": "a", "argv": ["python"], "cwd": str(project)},
        )
    with (
        SessionStore(root) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("Must not request model"))
        ) as http,
    ):
        conversation = Conversation(
            store, LLMClient(LLMConfig("https://example.test", "test", "secret"), http), ""
        )
        conversation.resume(session_id)
        with pytest.raises(StorageError, match="结果未知"):
            conversation.send("try again")
        assert store.usage(session_id) == (0, False)


@pytest.mark.parametrize("decision", ["y", "n"])
def test_approval_controls_actual_file_side_effect(project, tmp_path, decision):
    """同一写文件命令在批准时产生文件，在拒绝时没有副作用。"""
    payloads = []
    reply = tool_reply()
    reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('effect.txt').write_text('executed', encoding='utf-8')",
            ]
        }
    )

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=reply if len(payloads) == 1 else final_reply())

    inputs = iter(["write a marker", decision, "/exit"])
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=lambda _: None,
            workspace=project,
        )
        execution = store.load_executions(store.list_sessions()[0].session_id)[0]
        assert execution["status"] == ("completed" if decision == "y" else "denied")
    marker = project / "effect.txt"
    assert marker.exists() is (decision == "y")
    if decision == "y":
        assert marker.read_text(encoding="utf-8") == "executed"
    assert len(payloads) == (2 if decision == "y" else 1)
    if decision == "y":
        assert json.loads(payloads[1]["messages"][-1]["content"])["status"] == "completed"


def test_finished_command_log_failure_blocks_reexecution(project, tmp_path, monkeypatch):
    """进程已写文件但结束记录失败时，恢复后的未知状态阻止再次执行。"""
    root = tmp_path / "sessions"
    requests = []
    reply = tool_reply()
    reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; p=Path('effect.txt'); p.write_text(p.read_text()+'once' if p.exists() else 'once')",
            ]
        }
    )

    def handle(request):
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(200, json=reply)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with SessionStore(root) as store:
            append = store.append_command_event

            def fail_finish(session_id, kind, payload):
                if kind == "command_finished":
                    raise StorageError("结束记录保存失败")
                return append(session_id, kind, payload)

            monkeypatch.setattr(store, "append_command_event", fail_finish)
            conversation = Conversation(
                store,
                LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
                "",
                ToolRegistry(project, confirm_command=lambda _: True),
            )
            with pytest.raises(CommandLogError):
                conversation.send("write once")
            session_id = conversation.session.session_id
            assert store.load_executions(session_id)[0]["status"] == "unknown"
        with SessionStore(root) as store:
            conversation = Conversation(
                store,
                LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
                "",
                ToolRegistry(project, confirm_command=lambda _: True),
            )
            conversation.resume(session_id)
            with pytest.raises(StorageError):
                conversation.send("continue")
            assert store.load_messages(session_id) == []
    assert (project / "effect.txt").read_text() == "once"
    assert len(requests) == 1


def test_command_does_not_inherit_unapproved_secret_variable(project, monkeypatch):
    """父进程存在自定义密钥变量时，真实子进程仍只能看到允许继承的基础环境。"""
    monkeypatch.setenv("ADAM_TEST_SECRET", "dummy-private-value")
    result = invoke(
        ToolRegistry(project, confirm_command=lambda _: True),
        [sys.executable, "-c", "import os; print(os.environ.get('ADAM_TEST_SECRET', 'absent'))"],
    )
    assert result["exit_code"] == 0 and result["stdout"].strip() == "absent"

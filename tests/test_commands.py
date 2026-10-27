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
    returned = json.loads(payloads[1]["messages"][-1]["content"])
    assert returned["status"] == ("completed" if decision == "y" else "denied")
    with SessionStore(tmp_path / "sessions") as store:
        session = store.list_sessions()[0]
        executions = store.load_executions(session.session_id)
        assert len(executions) == 1
        assert executions[0]["status"] == returned["status"]
        assert executions[0]["tool_call_id"] == "command1"
        assert store.load_messages(session.session_id)[-1].content == "Command completed."


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
        assert store.load_executions(session_id)[0]["status"] == "completed"


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
        store.append_command_event(
            session_id,
            "command_started",
            {"execution_id": "a", "tool_call_id": "t", "argv": ["python"], "cwd": str(project)},
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

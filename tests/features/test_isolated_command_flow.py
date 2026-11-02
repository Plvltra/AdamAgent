"""B19.2.3：真实应用工具循环与保存；这里只替换 OS 后端，真实隔离另行验收。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import PurePosixPath

import httpx
import pytest

from adam.cli.main import confirm_command
from adam.config.loader import load_config
from adam.config.settings import (
    CommandIsolationConfig,
    PermissionConfig,
    ToolDiscoveryConfig,
    ToolLimits,
)
from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_execution import CommandInputPreview, CommandIsolationResult
from adam.contracts.errors import (
    ApprovalDeniedError,
    BudgetError,
    CommandLogError,
    ConfigError,
    IsolationError,
    NoProgressError,
    RequestInterrupted,
    StorageError,
    ToolHookError,
)
from adam.contracts.execution import ISOLATED_COMMAND_TOOL, ExecutionMode
from adam.contracts.models import CommandRequest
from adam.isolation.commands import IsolatedCommands
from adam.recording.runtime import replay_turn
from adam.session.feedback_log import failure_signal
from adam.session.log import SessionLog
from adam.session.storage import SessionStore
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.isolated_commands import register_isolated_commands
from adam.tool_calls.policy import PathPolicy
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import plan_app, step
from tests.features.test_tool_discovery_loop import names
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def enabled(root, **changes):
    return replace(config_at(root), command_isolation=CommandIsolationConfig(True), **changes)


def install_runner(monkeypatch, *, code=0, reason="exited", failure=None):
    """替换隔离进程边界并记录输入；禁止用此替身声称验证了 Landlock 或进程清理。"""
    calls = []

    def run(self, argv, *, timeout_seconds=5, inputs=(), cancel=None):
        calls.append((tuple(argv), timeout_seconds, inputs))
        if failure is not None:
            raise failure
        return CommandIsolationResult(
            code, reason, "first\n" + "x" * 6000 + "\nlast", "error-tail", 6021, 12
        )

    monkeypatch.setattr(IsolatedCommands, "run", run)
    monkeypatch.setattr(
        CommandTools, "run_command", lambda *args, **kwargs: pytest.fail("unexpected host fallback")
    )
    return calls


def projected(events):
    log = SessionLog(events[0]["payload"])
    log.extend(events[1:])
    return log


@pytest.mark.parametrize(
    "code,reason,status",
    [
        (0, "exited", "completed"),
        (7, "exited", "completed"),
        (-9, "wall_limit", "timed_out"),
        (-9, "output_limit", "output_limited"),
    ],
)
@pytest.mark.parametrize("limit", [512, 1000])
def test_discovery_plan_evidence_command_facts_and_zero_execution_replay(
    tmp_path, monkeypatch, code, reason, status, limit
):
    source = tmp_path / "note.txt"
    source.write_bytes(b"exact\r\nbytes")
    calls = install_runner(monkeypatch, code=code, reason=reason)
    config = enabled(
        tmp_path,
        tool_discovery=ToolDiscoveryConfig(),
        tool_limits=ToolLimits(max_result_chars=limit),
    )
    succeeded = code == 0 and reason == "exited"

    def respond(body, number):
        if number == 1:
            assert ISOLATED_COMMAND_TOOL not in names(body)
            value = tool_response(
                [call("discover", "discover_tools", names=[ISOLATED_COMMAND_TOOL, "update_plan"])]
            )
        elif number == 2:
            assert ISOLATED_COMMAND_TOOL in names(body)
            value = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call(
                        "command",
                        ISOLATED_COMMAND_TOOL,
                        argv=["cat", "src/note"],
                        inputs={"src/note": "note.txt"},
                    ),
                ]
            )
        elif number == 3:
            result = outputs(body)[-1]
            assert result["status"] == status and result["exit_code"] == code
            assert result["environment"] == "isolated"
            value = tool_response(
                [call("complete", "update_plan", steps=[step("completed", ["command"])])]
            )
        elif number == 4 and not succeeded:
            assert not outputs(body)[-1]["ok"]
            value = tool_response(
                [
                    call(
                        "failed",
                        "update_plan",
                        steps=[step("failed", ["command"], "actual command did not succeed")],
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["ok"]
            value = answer("recorded outcome")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, _):
        assert recorder.run(lambda: app.send("task")) == "recorded outcome"
        sid = app.session.session_id
        events = store.load_events(sid)
        log = projected(events)
        record = store.load_executions(sid)[0]
        assert record["environment"] == "isolated" and record["cwd"] == "/work"
        assert record["input_files"] == [
            {
                "name": "src/note",
                "source": str(source),
                "version": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ]
        assert record["status"] == status and record["reason"] == reason
        assert record["stdout"].startswith("first") and record["stdout"].endswith("last")
        fact = store.execution_report(sid, record["turn_id"]).actions[0]
        assert fact.status == status and fact.exit_code == code and "isolated" in fact.target
        assert failure_signal(log.tools[(record["turn_id"], "command")], log.executions) is (
            not succeeded
        )
        assert len(calls) == 1 and calls[0][2][0].data == source.read_bytes()
        used = [
            e["payload"]["context"]["runtime_state"]["tools"]["used"]
            for e in events
            if e["type"] == "request_started"
        ]
        assert used[-1] == (4 if succeeded else 5)
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_executions(sid)[0]["status"] == status
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        IsolatedCommands, "run", lambda *args, **kwargs: pytest.fail("replay ran backend")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize("decision", ["deny", "changed", "protected"])
def test_approval_and_input_changes_prevent_execution(tmp_path, monkeypatch, decision):
    calls = install_runner(monkeypatch)
    source = tmp_path / "note.txt"
    source.write_text("original", encoding="utf-8")
    if decision == "protected":
        source = tmp_path / "memory" / "secret.txt"
        source.parent.mkdir()
        source.write_text("internal", encoding="utf-8")
    approvals, bodies = [], []

    def confirm(command):
        approvals.append(command)
        if decision == "changed":
            source.write_text("external change", encoding="utf-8")
        return decision != "deny"

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        value = (
            tool_response(
                [
                    call(
                        "command",
                        ISOLATED_COMMAND_TOOL,
                        argv=["cat", "note"],
                        inputs={"note": str(source)},
                    )
                ]
            )
            if len(bodies) == 1
            else answer("stopped")
        )
        if len(bodies) == 2:
            assert not outputs(body)[-1]["ok"]
        return httpx.Response(200, json=value)

    with plan_app(tmp_path, respond, config=enabled(tmp_path), confirm=confirm) as (app, store):
        if decision == "deny":
            with pytest.raises(ApprovalDeniedError):
                app.send("task")
            assert len(bodies) == 1
        else:
            assert app.send("task") == "stopped"
        records = store.load_executions(app.session.session_id)
        assert [r["status"] for r in records] == (["denied"] if decision == "deny" else [])
    assert not calls and len(approvals) == (0 if decision == "protected" else 1)


@pytest.mark.parametrize(
    "failure,expected",
    [(IsolationError("backend failed"), "unknown"), (RequestInterrupted(), "interrupted")],
)
def test_backend_failure_or_cancel_stops_without_host_fallback(
    tmp_path, monkeypatch, failure, expected
):
    calls = install_runner(monkeypatch, failure=failure)

    def respond(body, number):
        assert number == 1
        return httpx.Response(
            200, json=tool_response([call("command", ISOLATED_COMMAND_TOOL, argv=["true"])])
        )

    with captured_app(tmp_path, respond, config=enabled(tmp_path)) as (
        app,
        recorder,
        store,
        path,
        _,
    ):
        with pytest.raises(type(failure)):
            recorder.run(lambda: app.send("task"))
        records = store.load_executions(app.session.session_id)
        assert records[0]["status"] == expected and records[0].get("exit_code") is None
        assert store.load_messages(app.session.session_id)[-1].content == "old answer"
    assert len(calls) == 1
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        IsolatedCommands, "run", lambda *args, **kwargs: pytest.fail("replay ran backend")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize("phase", ["command_started", "command_finished"])
def test_log_failure_preserves_unknown_outcome_and_never_retries(tmp_path, monkeypatch, phase):
    calls = install_runner(monkeypatch)

    def respond(body, number):
        assert number == 1
        return httpx.Response(
            200, json=tool_response([call("command", ISOLATED_COMMAND_TOOL, argv=["true"])])
        )

    with captured_app(tmp_path, respond, config=enabled(tmp_path)) as (app, recorder, store, _, _):
        original = store.append_command_event

        def save(sid, kind, payload):
            if kind == phase:
                raise StorageError("injected command journal failure")
            return original(sid, kind, payload)

        monkeypatch.setattr(store, "append_command_event", save)
        with pytest.raises(CommandLogError):
            recorder.run(lambda: app.send("task"))
        assert len(calls) == (phase == "command_finished")
        records = store.load_executions(app.session.session_id)
        assert [r["status"] for r in records] == (["unknown"] if calls else [])


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": []},
        {"argv": ["true"], "cwd": "."},
        {"argv": ["true"], "inputs": {"../escape": "note"}},
        {"argv": ["true"], "inputs": {"note": False}},
        {"argv": ["true"], "timeout_seconds": True},
        {"argv": ["true", "\ud800"]},
    ],
)
def test_invalid_arguments_do_not_reach_rules_hooks_or_approval(tmp_path, monkeypatch, arguments):
    calls = install_runner(monkeypatch)

    def forbidden(*args):
        pytest.fail("invalid arguments crossed preflight")

    registry = register_isolated_commands(
        create_tool_registry(tmp_path),
        IsolatedCommands(),
        PathPolicy(tmp_path, PermissionConfig()),
        "Ubuntu",
        confirm=forbidden,
    ).guarded(forbidden)
    result = json.loads(
        registry.execute(ISOLATED_COMMAND_TOOL, json.dumps(arguments), before_tool=forbidden)
    )
    assert not result["ok"] and not calls


def test_mode_and_precancel_refuse_before_approval(tmp_path, monkeypatch):
    calls = install_runner(monkeypatch)
    token = CancellationToken()
    approvals = []
    registry = register_isolated_commands(
        create_tool_registry(tmp_path),
        IsolatedCommands(),
        PathPolicy(tmp_path, PermissionConfig()),
        "Ubuntu",
        confirm=lambda c: approvals.append(c) or True,
        cancel=token,
    )
    plan = registry.with_mode(lambda: ExecutionMode.PLAN)
    assert (
        json.loads(plan.execute(ISOLATED_COMMAND_TOOL, '{"argv":["true"]}'))["status"]
        == "mode_blocked"
    )
    token.cancel()
    with pytest.raises(RequestInterrupted):
        registry.execute(ISOLATED_COMMAND_TOOL, '{"argv":["true"]}')
    assert not calls and not approvals


def test_hook_denial_sees_selected_paths_without_read_or_approval(tmp_path, monkeypatch):
    calls = install_runner(monkeypatch)
    selected = []

    def forbidden(*args, **kwargs):
        pytest.fail("blocked operation read an input or requested approval")

    def before(name, args, mode):
        assert name == ISOLATED_COMMAND_TOOL and mode is ExecutionMode.ACT
        raise ToolHookError("blocked by configured hook")

    monkeypatch.setattr("adam.tool_calls.isolated_commands.capture_inputs", forbidden)
    registry = register_isolated_commands(
        create_tool_registry(tmp_path),
        IsolatedCommands(),
        PathPolicy(tmp_path, PermissionConfig()),
        "Ubuntu",
        confirm=forbidden,
    ).guarded(lambda paths: selected.extend(paths))
    result = json.loads(
        registry.execute(
            ISOLATED_COMMAND_TOOL,
            '{"argv":["cat","note"],"inputs":{"note":"note.txt"}}',
            before_tool=before,
        )
    )
    assert result["status"] == "hook_blocked" and [str(path) for path in selected] == ["note.txt"]
    assert not calls


def test_original_tool_budget_rejects_oversized_batch_before_execution(tmp_path, monkeypatch):
    calls = install_runner(monkeypatch)
    config = enabled(tmp_path)
    config = replace(config, agent=replace(config.agent, max_tool_calls=1))

    def respond(body, number):
        assert number == 1
        return httpx.Response(
            200,
            json=tool_response(
                [
                    call("first", ISOLATED_COMMAND_TOOL, argv=["true"]),
                    call("second", ISOLATED_COMMAND_TOOL, argv=["false"]),
                ]
            ),
        )

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, _, _):
        with pytest.raises(BudgetError):
            recorder.run(lambda: app.send("task"))
        records = store.load_executions(app.session.session_id)
        assert not records and not calls


def test_tiny_result_budget_keeps_isolated_failure_warning_and_stop(tmp_path, monkeypatch):
    calls = install_runner(monkeypatch, code=7)
    config = enabled(tmp_path, tool_limits=ToolLimits(max_result_chars=512))

    def respond(body, number):
        return httpx.Response(
            200,
            json=tool_response([call(f"command-{number}", ISOLATED_COMMAND_TOOL, argv=["false"])]),
        )

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, _, _):
        with pytest.raises(NoProgressError):
            recorder.run(lambda: app.send("task"))
        results = [
            e["payload"]["output"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "tool_finished"
        ]
        assert len(results) == len(calls) == 3 and all(len(value) <= 512 for value in results)
        values = [json.loads(value) for value in results]
        assert [value["no_progress"]["action"] for value in values] == ["continue", "warn", "stop"]
        for value in values:
            assert value["environment"] == "isolated" and value["reason"] == "exited"
            assert value["exit_code"] == 7 and value["artifact"]["complete"]
            assert value["preview_omitted_chars"]["stdout"] > 0


def test_cli_shows_environment_and_escaped_versioned_sources():
    shown = []
    command = CommandRequest(
        ("cat", "note"),
        PurePosixPath("/work"),
        5,
        "isolated",
        "Ubuntu",
        (CommandInputPreview("note", "source\nforged", "a" * 64),),
    )
    assert confirm_command(command, lambda _: "y", shown.append)
    line = next(s for s in shown if s.startswith("输入快照> "))
    assert "\n" not in line and json.loads(line.split("> ", 1)[1])["source"] == "source\nforged"
    assert any("隔离" in line and "无网络" in line for line in shown)


@pytest.mark.parametrize(
    "section",
    ['enabled="yes"', 'distribution="--unsafe"', 'distribution="Ubuntu /bin/sh"', "fallback=true"],
)
def test_config_rejects_ambiguous_backend_or_fallback(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text("[command_isolation]\n" + section, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError, match="command_isolation"):
        load_config(path)


def test_config_defaults_disabled_and_selects_distribution(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    assert not load_config(path).command_isolation.enabled
    path.write_text(
        '[command_isolation]\nenabled=true\ndistribution="Ubuntu-24.04"', encoding="utf-8"
    )
    assert load_config(path).command_isolation == CommandIsolationConfig(True, "Ubuntu-24.04")


def test_finished_log_cannot_change_approved_source_or_claim_host(tmp_path, monkeypatch):
    install_runner(monkeypatch)

    def respond(body, number):
        value = (
            tool_response([call("command", ISOLATED_COMMAND_TOOL, argv=["true"])])
            if number == 1
            else answer("done")
        )
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=enabled(tmp_path)) as (app, recorder, store, _, _):
        recorder.run(lambda: app.send("task"))
        events = store.load_events(app.session.session_id)
    for key, replacement in [
        ("environment", "host"),
        ("input_files", [{"name": "forged"}]),
        ("reason", "exited_but_forged"),
        ("exit_code", True),
    ]:
        altered = deepcopy(events)
        finish = next(e["payload"] for e in altered if e["type"] == "command_finished")
        finish[key] = replacement
        with pytest.raises(ValueError):
            projected(altered)


def test_disabled_parent_and_enabled_child_do_not_expose_isolated_command(tmp_path, monkeypatch):
    calls = install_runner(monkeypatch)

    def ordinary(body, number):
        assert ISOLATED_COMMAND_TOOL not in names(body)
        return httpx.Response(200, json=answer("ordinary"))

    with captured_app(tmp_path, ordinary) as (app, recorder, _, _, _):
        assert recorder.run(lambda: app.send("task")) == "ordinary"

    def child(request):
        body = json.loads(request.content)
        assert ISOLATED_COMMAND_TOOL not in names(body)
        return httpx.Response(200, json=answer("parent must execute"))

    def parent(body, number):
        if number == 1:
            value = tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
        elif number == 2:
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["reply"] == "parent must execute"
            value = answer("done")
        return httpx.Response(200, json=value)

    second = tmp_path / "enabled"
    second.mkdir()
    with captured_app(second, parent, config=enabled(second), child_handler=child) as (
        app,
        recorder,
        _,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "done"
    assert not calls

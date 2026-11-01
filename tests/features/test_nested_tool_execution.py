"""B14.2.1：显式编排入口经过真实执行器；固定宿主回调不代表 OS 隔离验收。"""

import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from adam.application.conversation import Conversation
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import (
    ApprovalDeniedError,
    BudgetError,
    NoProgressError,
    RequestInterrupted,
    StorageError,
)
from adam.contracts.execution import CODE_TOOL_NAME, ExecutionMode, ToolCapability
from adam.contracts.hooks import HookEvent, HookResult
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter
from adam.recording.runtime import CaptureRuntime, replay_turn
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import forbid_external_actions


@contextmanager
def nested_conversation(
    root, operation, *, respond=None, config=None, approved=True, hooks=None, mode=ExecutionMode.ACT
):
    """固定编排回调直接调真实底层工具；模型仅替换 HTTP，日志和录制均实际落盘。"""
    config = config or config_at(root)
    path = root / "nested.recording.jsonl"
    bodies, bindings, approvals = [], [], []
    token = CancellationToken()

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        value = (
            respond(body, len(bodies))
            if respond
            else (
                tool_response([call("parent", CODE_TOOL_NAME)])
                if len(bodies) == 1
                else answer("done")
            )
        )
        return httpx.Response(200, json=value)

    def approve(plan):
        approvals.append(plan)
        return approved

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        sid = store.create_session("nested", config.system_prompt)
        store.select_session(sid)
        with RecordingWriter(
            path, capture_seed(config, store, sid, "task", root, mode=mode)
        ) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            runtime = CaptureRuntime(recorder)
            registry = create_tool_registry(root, confirm_edit=approve, confirm_command=approve)
            registry = registry.extended(
                {CODE_TOOL_NAME: schema("test host orchestration", {}, [])},
                {CODE_TOOL_NAME: lambda args, record: operation(executor, token)},
                capabilities={CODE_TOOL_NAME: ToolCapability.DELEGATE},
            ).with_mode(lambda: mode)
            executor = ToolExecutor(
                store,
                registry,
                lambda _: None,
                before_call=bindings.append,
                cancel=token,
                hooks=hooks,
            )
            conversation = Conversation(
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                config.system_prompt,
                runtime.tools(executor),
                config.agent,
                model=config.llm.model,
                context_config=config.context,
                mode_provider=lambda: mode,
            )
            runtime.bind(conversation)
            conversation.resume(sid)
            yield SimpleNamespace(
                run=lambda: recorder.run(lambda: conversation.send("task")),
                conversation=conversation,
                executor=executor,
                store=store,
                sid=sid,
                path=path,
                bodies=bodies,
                bindings=bindings,
                approvals=approvals,
                cancel=token,
            )


def records(env, kind):
    return [e["payload"] for e in env.store.load_events(env.sid) if e["type"] == kind]


def read_and_count(executor, cancel):
    info = executor.execute_nested("file_info", {"path": "a.txt"})
    content = executor.execute_nested("read_file", {"path": "a.txt"})
    return {"size": info["size"], "lines": len(content["lines"])}


class InspectHooks:
    """调用真实输入编码校验；模拟本地检查决定，不启动额外 Hook 进程。"""

    def __init__(self, workspace, *, deny_read=False):
        self.workspace, self.deny_read, self.requests = workspace, deny_read, []

    def configured(self, event):
        return True

    def run(self, request):
        request.encode(str(self.workspace), 16000)
        self.requests.append(request)
        if request.event is HookEvent.AFTER_TOOL:
            return HookResult("observed", exit_code=0)
        return HookResult(
            "denied" if self.deny_read and request.tool_name == "read_file" else "allowed",
            exit_code=0,
        )


def test_nested_tools_keep_sources_hooks_and_restart_without_adding_raw_messages(
    tmp_path, monkeypatch
):
    text = "private intermediate line\nsecond line\n"
    (tmp_path / "a.txt").write_text(text, encoding="utf-8", newline="\n")
    hooks = InspectHooks(tmp_path)
    with nested_conversation(tmp_path, read_and_count, hooks=hooks) as env:
        assert env.run() == "done"
        started = records(env, "tool_started")
        assert [e["name"] for e in started] == [CODE_TOOL_NAME, "file_info", "read_file"]
        assert len({e["tool_call_id"] for e in started}) == 3
        assert all(e["parent_tool_call_id"] == "parent" for e in started[1:])
        assert len({(e["turn_id"], e["request_id"]) for e in started}) == 1
        assert [e["tool_call_id"] for e in records(env, "tool_finished")] == [
            started[1]["tool_call_id"],
            started[2]["tool_call_id"],
            "parent",
        ]
        assert [r.tool_name for r in hooks.requests] == [
            CODE_TOOL_NAME,
            "file_info",
            "file_info",
            "read_file",
            "read_file",
            CODE_TOOL_NAME,
        ]
        assert env.bindings[-1]["tool_call_id"] == "parent"
        assert outputs(env.bodies[1]) == [{"ok": True, "size": len(text), "lines": 2}]
        assert "private intermediate line" not in json.dumps(env.bodies[1]["messages"])
        sid, events = env.sid, env.store.load_events(env.sid)
    with SessionStore(tmp_path / "sessions") as reopened:
        assert reopened.load_events(sid) == events
        assert reopened.load_messages(sid)[-1].content == "done"
    # 删除原文件，并禁用网络/进程；重放只能使用事件，不能重新读取或执行编排。
    (tmp_path / "a.txt").unlink()
    forbid_external_actions(monkeypatch)
    report = replay_turn(env.path, tmp_path / "replay")
    assert report["status"] == "matched" and report["reply"] == "done"


@pytest.mark.parametrize("later_batch", [False, True])
def test_shared_tool_budget_stops_nested_or_later_batch_and_replays_same_failure(
    tmp_path, monkeypatch, later_batch
):
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    config = config_at(tmp_path)
    config = replace(config, agent=replace(config.agent, max_tool_calls=4 if later_batch else 2))

    def respond(body, count):
        if count == 1:
            return tool_response([call("parent", CODE_TOOL_NAME)])
        return tool_response(
            [call("later1", "file_info", path="a.txt"), call("later2", "read_file", path="a.txt")]
        )

    with nested_conversation(tmp_path, read_and_count, config=config, respond=respond) as env:
        with pytest.raises(BudgetError) as error:
            env.run()
        assert error.value.code.value == "tool_limit"
        assert len(records(env, "tool_started")) == (3 if later_batch else 2)
        assert len(env.bodies) == (2 if later_batch else 1)
        assert env.store.load_messages(env.sid) == []
        assert records(env, "turn_finished")[-1]["reason"] == "tool_limit"
    forbid_external_actions(monkeypatch)
    report = replay_turn(env.path, tmp_path / "replay")
    assert report["status"] == "matched" and report["error"]["code"] == "tool_limit"


@pytest.mark.parametrize("approved", [True, False])
def test_nested_edit_keeps_approval_checkpoint_and_source(tmp_path, approved):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")

    def edit(executor, cancel):
        source = executor.execute_nested("read_file", {"path": "a.txt"})
        return executor.execute_nested(
            "replace_text",
            {
                "path": "a.txt",
                "old_text": "before",
                "new_text": "after",
                "expected_version": source["version"],
            },
        )

    with nested_conversation(tmp_path, edit, approved=approved) as env:
        if approved:
            assert env.run() == "done"
        else:
            with pytest.raises(ApprovalDeniedError):
                env.run()
        assert len(env.approvals) == 1
        edit_call = records(env, "tool_started")[-1]
        finished = records(env, "edit_finished")[-1]
        assert finished["tool_call_id"] == edit_call["tool_call_id"]
        assert edit_call["parent_tool_call_id"] == "parent"
        assert finished["status"] == ("applied" if approved else "denied")
        assert bool(records(env, "edit_started")) == approved
    assert target.read_text(encoding="utf-8") == ("after" if approved else "before")


def test_nested_edit_attempts_share_outer_edit_limit_even_on_invalid_arguments(tmp_path):
    config = config_at(tmp_path)
    config = replace(config, agent=replace(config.agent, max_edit_attempts=1))

    def attempt(executor, cancel):
        first = executor.execute_nested("replace_text", {})
        assert not first["ok"]
        return executor.execute_nested("replace_text", {})

    with nested_conversation(tmp_path, attempt, config=config) as env:
        with pytest.raises(BudgetError) as error:
            env.run()
        assert error.value.code.value == "edit_limit"
        assert len(records(env, "tool_started")) == 2
        assert env.approvals == []


@pytest.mark.parametrize("boundary", ["plan", "path", "hook", "recursive"])
def test_nested_calls_keep_mode_path_hooks_and_no_recursion(tmp_path, boundary):
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    config = config_at(tmp_path)
    mode = ExecutionMode.PLAN if boundary == "plan" else ExecutionMode.ACT
    hooks = InspectHooks(tmp_path, deny_read=boundary == "hook")

    def rejected(executor, cancel):
        if boundary == "recursive":
            return executor.execute_nested(CODE_TOOL_NAME, {})
        if boundary == "plan":
            return executor.execute_nested(
                "replace_text",
                {
                    "path": "a.txt",
                    "old_text": "before",
                    "new_text": "after",
                    "expected_version": "a" * 64,
                },
            )
        return executor.execute_nested(
            "read_file", {"path": "../outside.txt" if boundary == "path" else "a.txt"}
        )

    with nested_conversation(tmp_path, rejected, config=config, hooks=hooks, mode=mode) as env:
        assert env.run() == "done"
        result = outputs(env.bodies[-1])[-1]
        assert not result["ok"]
        if boundary in {"plan", "hook"}:
            assert result["status"] == {"plan": "mode_blocked", "hook": "hook_blocked"}[boundary]
        else:
            assert "error" in result
        assert len(records(env, "tool_started")) == 2 and not env.approvals
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "before"


def test_cancel_stops_next_nested_call_and_stale_callback_has_no_authority(tmp_path):
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")

    def cancel_after_read(executor, token):
        executor.execute_nested("read_file", {"path": "a.txt"})
        token.cancel()
        return executor.execute_nested("file_info", {"path": "a.txt"})

    with nested_conversation(tmp_path, cancel_after_read) as env:
        with pytest.raises(RequestInterrupted):
            env.run()
        assert len(records(env, "tool_started")) == 2
        with pytest.raises(RuntimeError, match="本线程"):
            env.executor.execute_nested("file_info", {"path": "a.txt"})


def test_other_thread_cannot_use_parent_capability(tmp_path):
    errors = []

    def operation(executor, cancel):
        def cross_thread():
            try:
                executor.execute_nested("read_file", {"path": "a.txt"})
            except RuntimeError as error:
                errors.append(str(error))

        thread = Thread(target=cross_thread)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive()
        return {"stopped": bool(errors)}

    with nested_conversation(tmp_path, operation) as env:
        assert env.run() == "done" and len(errors) == 1
        assert len(records(env, "tool_started")) == 1


@pytest.mark.parametrize("kind", ["tool_started", "tool_finished"])
def test_nested_log_failure_stops_parent_without_retry(tmp_path, monkeypatch, kind):
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    with nested_conversation(tmp_path, read_and_count) as env:
        original = env.store.append_tool_event
        attempts = []

        def fail_nested(sid, event, payload):
            if event == kind and "parent_tool_call_id" in payload:
                attempts.append(payload["tool_call_id"])
                raise StorageError("injected nested save failure")
            original(sid, event, payload)

        monkeypatch.setattr(env.store, "append_tool_event", fail_nested)
        with pytest.raises(StorageError, match="nested save"):
            env.run()
        assert len(attempts) == 1 and len(env.bodies) == 1
        assert len(records(env, "tool_started")) == (1 if kind == "tool_started" else 2)
        assert records(env, "tool_finished") == []


def test_nested_repeated_failures_use_existing_no_progress_stop(tmp_path):
    def repeated(executor, cancel):
        for _ in range(10):
            executor.execute_nested("read_file", {"path": "missing.txt"})
        pytest.fail("repeated failure did not stop")

    with nested_conversation(tmp_path, repeated) as env:
        with pytest.raises(NoProgressError):
            env.run()
        assert len(env.bodies) == 1
        assert records(env, "turn_finished")[-1]["reason"] == "no_progress"
        assert len(records(env, "tool_started")) < 11

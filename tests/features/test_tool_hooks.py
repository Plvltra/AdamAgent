"""T5.2：模拟模型 HTTP，实际运行本地检查器、文件工具与命令，并核对独立日志。"""

import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.application.conversation import Conversation
from adam.bootstrap import build_application
from adam.config.hooks import HooksConfig
from adam.config.settings import AgentConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import (
    ApprovalDeniedError,
    HookLogError,
    RequestInterrupted,
    StorageError,
)
from adam.hooks.runner import HookRunner
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_cancellation import alive
from tests.features.test_hook_process import checker
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def hooks_at(root, *, before="allow", after="ok"):
    """真实脚本记录收到的输入；bad 模拟协议故障，其他场景使用合法响应。"""
    program = checker(
        root,
        "import json, sys\nfrom pathlib import Path\n"
        "data = json.load(sys.stdin)\n"
        "name = data['event'] + '-' + data['source']['tool_call_id'] + '.json'\n"
        "Path(name).write_text(json.dumps(data), encoding='utf-8')\n"
        f"choice = {before!r} if data['event'] == 'before_tool' else {after!r}\n"
        "if choice == 'bad':\n    print('broken JSON')\n"
        "elif data['event'] == 'before_tool':\n"
        "    print(json.dumps({'version': 1, 'decision': choice, 'reason': 'local policy'}))\n"
        "else:\n    print(json.dumps({'version': 1, 'note': 'observed actual result'}))\n",
    )
    return HooksConfig(enabled=True, before_tool=program, after_tool=program)


@contextmanager
def hooked_app(root, handler, hooks, *, notices=None, approve=None):
    config = replace(config_at(root), hooks=hooks)
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            root,
            confirm_command=approve,
            confirm_edit=approve,
            on_notice=(notices if notices is not None else []).append,
        )
        try:
            yield app, store
        finally:
            app.close()


def model_for(calls, payloads):
    """两次模型请求夹住真实工具批次，后续请求保留工具实际回传内容。"""

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json=tool_response(calls) if len(payloads) == 1 else answer("done")
        )

    return respond


def command_call(marker):
    return call(
        "action",
        "run_command",
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
    )


def test_child_inherits_hooks_and_preserves_mode_source_and_warning(tmp_path):
    """子线程使用独立检查来源与日志，后置失败通过带子任务 ID 的提示报告。"""
    (tmp_path / "note.txt").write_text("child evidence")
    config = replace(config_at(tmp_path), hooks=hooks_at(tmp_path, after="bad"))
    parent_requests, child_requests, notices = [], [], []

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        if len(parent_requests) == 1:
            return httpx.Response(
                200,
                json=tool_response([call("spawn", "spawn_agent", name="reader", task="read note")]),
            )
        if len(parent_requests) == 2:
            return httpx.Response(
                200, json=tool_response([call("wait", "wait_agents", agent_ids=[])])
            )
        assert outputs(body)[-1]["agents"][0]["status"] == "completed"
        return httpx.Response(200, json=answer("collected"))

    with application(
        tmp_path,
        parent,
        model_for([call("child-read", "read_file", path="note.txt")], child_requests),
        config=config,
        notices=notices,
    ) as (app, store):
        app.select_mode("plan")
        assert app.send("delegate read") == "collected"
        index_path = index_for(app)
        child_id = json.loads(index_path.read_text())["agents"][0]["agent_id"]
        parent_records = store.load_hooks(app.session.session_id)
        with SessionStore(index_path.parent / "sessions") as children:
            child_records = children.load_hooks(child_id)
        assert len(parent_records) == 4 and len(child_records) == 2
        assert {r["execution_mode"] for r in parent_records + child_records} == {"plan"}
        assert not {r["turn_id"] for r in parent_records} & {r["turn_id"] for r in child_records}
        assert [r["status"] for r in child_records] == ["allowed", "failed"]
        assert any(child_id in line and "后置 Hook 检查失败" in line for line in notices)
        received = json.loads((tmp_path / "after_tool-child-read.json").read_text())
        assert received["source"]["session_id"] == child_id
        assert "child evidence" in json.dumps(outputs(child_requests[1]))


def test_before_and_after_are_linked_and_after_reads_durable_tool_result(tmp_path):
    (tmp_path / "note.txt").write_text("actual evidence", encoding="utf-8")
    hooks = hooks_at(tmp_path)
    payloads = []
    with hooked_app(
        tmp_path, model_for([call("read", "read_file", path="note.txt")], payloads), hooks
    ) as (app, store):
        assert app.send("read") == "done"
        sid = app.session.session_id
        entries = store.load_events(sid)
        records = store.load_hooks(sid)
        assert [r["status"] for r in records] == ["allowed", "observed"]
        assert len({r["turn_id"] for r in records}) == 1
        assert len({r["request_id"] for r in records}) == 1
        assert {r["tool_call_id"] for r in records} == {"read"}
        tool_end = next(e for e in entries if e["type"] == "tool_finished")
        after_start = next(
            e
            for e in entries
            if e["type"] == "hook_started" and e["payload"]["event"] == "after_tool"
        )
        assert entries.index(tool_end) < entries.index(after_start)
        received = json.loads((tmp_path / "after_tool-read.json").read_text())
        assert received["result"] == outputs(payloads[1])[-1]
        assert received["source"] == {
            key: records[0][key] for key in ("turn_id", "request_id", "tool_call_id")
        } | {"session_id": sid}
        assert tool_end["payload"]["output"] == payloads[1]["messages"][-1]["content"]
        assert "actual evidence" in tool_end["payload"]["output"]
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_hooks(sid) == records
        assert len(reopened.load_messages(sid)) == 4


@pytest.mark.parametrize("decision,status", [("deny", "denied"), ("bad", "failed")])
def test_before_rejection_or_failure_blocks_command_without_approval(tmp_path, decision, status):
    marker = tmp_path / "ran"
    approvals, payloads = [], []
    with hooked_app(
        tmp_path,
        model_for([command_call(marker)], payloads),
        hooks_at(tmp_path, before=decision),
        approve=lambda request: approvals.append(request) or True,
    ) as (app, store):
        assert app.send("run") == "done"
        sid = app.session.session_id
        result = outputs(payloads[1])[-1]
        assert result["ok"] is False and result["status"] == "hook_blocked"
        assert approvals == [] and not marker.exists()
        assert not (tmp_path / "after_tool-action.json").exists()
        assert store.load_executions(sid) == []
        assert [r["status"] for r in store.load_hooks(sid)] == [status]
        turn = store.load_hooks(sid)[0]["turn_id"]
        assert store.feedback_sources(sid, turn)["failures"] == []


def test_after_failure_preserves_real_command_success_and_reports_it(tmp_path):
    marker = tmp_path / "ran"
    payloads, notices = [], []
    with hooked_app(
        tmp_path,
        model_for([command_call(marker)], payloads),
        hooks_at(tmp_path, after="bad"),
        notices=notices,
        approve=lambda _: True,
    ) as (app, store):
        assert app.send("run") == "done"
        sid = app.session.session_id
        assert marker.read_text() == "ran"
        assert outputs(payloads[1])[-1]["exit_code"] == 0
        assert outputs(payloads[1])[-1]["status"] == "completed"
        assert store.load_executions(sid)[0]["status"] == "completed"
        assert [r["status"] for r in store.load_hooks(sid)] == ["allowed", "failed"]
        assert any("后置 Hook 检查失败" in notice for notice in notices)
        assert len([e for e in store.load_events(sid) if e["type"] == "command_started"]) == 1


def test_hook_allow_does_not_replace_user_approval(tmp_path):
    marker, payloads = tmp_path / "ran", []
    with hooked_app(
        tmp_path,
        model_for([command_call(marker)], payloads),
        hooks_at(tmp_path),
        approve=lambda _: False,
    ) as (app, store):
        with pytest.raises(ApprovalDeniedError):
            app.send("run")
        assert not marker.exists() and len(payloads) == 1
        records = store.load_hooks(app.session.session_id)
        assert [r["status"] for r in records] == ["allowed", "observed"]
        assert (
            json.loads((tmp_path / "after_tool-action.json").read_text())["result"]["status"]
            == "denied"
        )
        assert store.load_messages(app.session.session_id) == []


@pytest.mark.parametrize("mode,extra", [("plan", {}), ("act", {"invented": True})])
def test_invalid_mode_or_arguments_never_launch_hooks(tmp_path, mode, extra):
    payloads = []
    candidate = command_call(tmp_path / "ran")
    candidate = (candidate[0], candidate[1], json.dumps({**json.loads(candidate[2]), **extra}))
    with hooked_app(
        tmp_path,
        model_for([candidate], payloads),
        hooks_at(tmp_path),
        approve=lambda _: pytest.fail(),
    ) as (app, store):
        app.select_mode(mode)
        assert app.send("run") == "done"
        assert store.load_hooks(app.session.session_id) == []
        assert not list(tmp_path.glob("*_tool-*.json"))


@pytest.mark.parametrize(
    "phase,kind",
    [
        ("before_tool", "hook_started"),
        ("before_tool", "hook_finished"),
        ("after_tool", "hook_started"),
        ("after_tool", "hook_finished"),
    ],
)
def test_hook_log_failure_stops_without_redo_and_preserves_actual_tool_result(
    tmp_path, monkeypatch, phase, kind
):
    """只在公开日志写入边界注入磁盘故障，检查器和命令仍实际执行。"""
    marker, payloads = tmp_path / "ran", []
    with hooked_app(
        tmp_path,
        model_for([command_call(marker)], payloads),
        hooks_at(tmp_path),
        approve=lambda _: True,
    ) as (app, store):
        append = store.append_hook_event

        def fail(session_id, event, payload):
            if event == kind and payload["event"] == phase:
                raise StorageError("simulated disk failure")
            append(session_id, event, payload)

        monkeypatch.setattr(store, "append_hook_event", fail)
        with pytest.raises(HookLogError):
            app.send("run")
        sid = app.session.session_id
        assert len(payloads) == 1 and store.load_messages(sid) == []
        assert marker.exists() == (phase == "after_tool")
        events = store.load_events(sid)
        assert len([e for e in events if e["type"] == "tool_finished"]) == (phase == "after_tool")
        assert events[-1]["payload"]["reason"] == "hook_log_error"
        if kind == "hook_started":
            assert not (tmp_path / (phase + "-action.json")).exists()
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        records = reopened.load_hooks(sid)
        if kind == "hook_finished":
            assert records[-1]["status"] == "interrupted"
            assert records[-1]["duration_ms"] is None
            assert records[-1]["error_code"] == "restart_recovery"
        if phase == "after_tool":
            assert reopened.load_executions(sid)[0]["status"] == "completed"
        assert len(payloads) == 1


@pytest.mark.parametrize("phase", ["before_tool", "after_tool"])
def test_cancel_real_hook_reaps_process_and_preserves_completed_action(tmp_path, phase):
    """令牌取消发生在真实检查进程就绪后，不用伪造进行中日志代替实际执行。"""
    ready, marker = tmp_path / "hook.pid", tmp_path / "ran"
    command = checker(
        tmp_path,
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "json.load(sys.stdin)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    hooks = HooksConfig(enabled=True, **{phase: command}, timeout_seconds=20)
    cancel, payloads = CancellationToken(), []
    config = config_at(tmp_path)
    watcher_errors = []

    def stop():
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            watcher_errors.append("hook did not start")
        cancel.cancel()

    watcher = threading.Thread(target=stop)
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(
            transport=httpx.MockTransport(model_for([command_call(marker)], payloads))
        ) as http,
    ):
        client = LLMClient(config.llm, http)
        executor = ToolExecutor(
            store,
            create_tool_registry(tmp_path, confirm_command=lambda _: True),
            lambda _: None,
            cancel=cancel,
            hooks=HookRunner(hooks, tmp_path, cancel),
        )
        conversation = Conversation(
            store, client, "", executor, AgentConfig(), model="test", cancel=cancel
        )
        watcher.start()
        try:
            with pytest.raises(RequestInterrupted):
                conversation.send("run")
        finally:
            cancel.cancel()
            watcher.join(12)
        assert not watcher.is_alive() and watcher_errors == []
        assert not alive(int(ready.read_text()))
        sid = conversation.session.session_id
        records = store.load_hooks(sid)
        assert records[0]["status"] == "interrupted" and records[0]["error_code"] == "cancelled"
        assert marker.exists() == (phase == "after_tool")
        assert len(payloads) == 1 and store.load_messages(sid) == []
        assert len([e for e in store.load_events(sid) if e["type"] == "tool_finished"]) == (
            phase == "after_tool"
        )
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_hooks(sid) == records
        assert len(payloads) == 1

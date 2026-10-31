"""T6.2：模拟远端模型，实际读取、编辑、命令和 Hook 经过生产调度及 JSONL。"""

import hashlib
import json
import sys
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier, Event, Lock, current_thread, get_ident

import httpx
import pytest

from adam.application.conversation import Conversation
from adam.bootstrap import build_application
from adam.contracts.errors import ApprovalDeniedError, StorageError, ToolCleanupError
from adam.contracts.execution import ExecutionMode, ToolBudget, ToolCapability
from adam.hooks.runner import HookRunner
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.file_tools import FileTools
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call
from tests.features.test_tool_hooks import hooks_at, model_for


@contextmanager
def conversation_at(root, calls, registry, *, limits=None, cancel=None, before_call=None):
    config = config_at(root)
    payloads = []
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(model_for(calls, payloads))) as http,
    ):
        executor = ToolExecutor(
            store, registry, lambda _: None, cancel=cancel, before_call=before_call
        )
        conversation = Conversation(
            store,
            LLMClient(config.llm, http),
            "",
            executor,
            limits or config.agent,
            model=config.llm.model,
            cancel=cancel,
        )
        yield conversation, store, payloads


@pytest.mark.parametrize("with_hooks", [False, True])
def test_production_reads_overlap_but_hooks_and_logs_stay_on_coordinator(
    tmp_path, monkeypatch, with_hooks
):
    for name in ["a", "b", "c"]:
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
    coordinator = get_ident()
    pair, third_started = Barrier(2), Event()
    finished, readers, audit, hook_threads = [], [], [], []
    read_file = FileTools.read_file
    run_hook = HookRunner.run

    def read(self, path, arguments):
        readers.append(current_thread())
        if path.stem in {"a", "b"}:
            pair.wait(timeout=6)
        if path.stem == "a":
            assert third_started.wait(6)
        if path.stem == "c":
            third_started.set()
        value = read_file(self, path, arguments)
        finished.append(path.stem)
        return value

    def hook(self, request):
        hook_threads.append(get_ident())
        assert get_ident() == coordinator
        return run_hook(self, request)

    monkeypatch.setattr(FileTools, "read_file", read)
    monkeypatch.setattr(HookRunner, "run", hook)
    config = config_at(tmp_path)
    if with_hooks:
        config = replace(config, hooks=hooks_at(tmp_path))
    payloads = []
    calls = [call(name, "read_file", path=f"{name}.txt") for name in ["a", "b", "c"]]
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(model_for(calls, payloads))) as http,
    ):
        for method in ["append_tool_event", "append_hook_event"]:
            original = getattr(store, method)

            def record(sid, kind, data, original=original):
                assert get_ident() == coordinator
                audit.append((kind, data["tool_call_id"]))
                return original(sid, kind, data)

            monkeypatch.setattr(store, method, record)
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            assert app.send("read all three files") == "done"
            sid = app.session.session_id
            assert store.usage(sid) == (24, False)
            assert len(store.load_hooks(sid)) == (6 if with_hooks else 0)
        finally:
            app.close()
    assert finished.index("b") < finished.index("a")
    assert readers and all(t.ident != coordinator and not t.is_alive() for t in readers)
    assert hook_threads == ([coordinator] * 6 if with_hooks else [])
    for kind in ["tool_started", "tool_finished"]:
        assert [call_id for event, call_id in audit if event == kind] == ["a", "b", "c"]
    messages = [m for m in payloads[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in messages] == ["a", "b", "c"]
    assert [json.loads(m["content"])["lines"][0]["text"] for m in messages] == ["a", "b", "c"]
    with SessionStore(config.sessions_dir) as reopened:
        assert [m.raw["tool_call_id"] for m in reopened.load_messages(sid) if m.role == "tool"] == [
            "a",
            "b",
            "c",
        ]


@pytest.mark.parametrize("approve", [True, False])
def test_mixed_batch_keeps_stateful_tools_edits_and_commands_serial(tmp_path, monkeypatch, approve):
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    for name in ["a", "b"]:
        (tmp_path / f"{name}.txt").write_text(name)
    pair = Barrier(2)
    coordinator = get_ident()
    mutex = Lock()
    active = 0
    observed, source = [], {}
    read_file = FileTools.read_file

    def read(self, path, args):
        nonlocal active
        if path.stem in {"a", "b"}:
            with mutex:
                active += 1
            try:
                pair.wait(timeout=4)
                return read_file(self, path, args)
            finally:
                with mutex:
                    active -= 1
        assert (tmp_path / "ran").read_text() == "ran"
        return read_file(self, path, args)

    def checkpoint(label):
        assert get_ident() == coordinator and active == 0
        observed.append(label)

    def confirm(request):
        checkpoint("approve")
        return approve

    def stateful(args, record):
        checkpoint("stateful")
        assert source["tool_call_id"] == "state"
        return {"status": "ready", "text": target.read_text()}

    def encode(value):
        checkpoint("encode")
        return json.dumps(value)

    monkeypatch.setattr(FileTools, "read_file", read)
    registry = create_tool_registry(tmp_path, confirm_edit=confirm, confirm_command=confirm)
    registry = registry.extended(
        {"stateful_read": schema("Stateful extension", {}, [])},
        {"stateful_read": stateful},
        {"stateful_read": encode},
        capabilities={"stateful_read": ToolCapability.READ_ONLY},
    )
    calls = [
        call("a", "read_file", path="a.txt"),
        call("b", "read_file", path="b.txt"),
        call("state", "stateful_read"),
        call(
            "edit",
            "replace_text",
            path="target.txt",
            old_text="old",
            new_text="new",
            expected_version=hashlib.sha256(b"old").hexdigest(),
        ),
        call(
            "command",
            "run_command",
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('target.txt').read_text() == 'new'; "
                "Path('ran').write_text('ran')",
            ],
        ),
        call("after", "read_file", path="target.txt"),
    ]
    with conversation_at(
        tmp_path, calls, registry, before_call=lambda link: source.update(link)
    ) as (conversation, store, payloads):
        for method in ["append_tool_event", "append_edit_event", "append_command_event"]:
            original = getattr(store, method)

            def record(sid, kind, data, original=original):
                assert get_ident() == coordinator
                return original(sid, kind, data)

            monkeypatch.setattr(store, method, record)
        if approve:
            assert conversation.send("read, edit and check") == "done"
            assert observed == ["stateful", "encode", "approve", "approve"]
            assert target.read_text() == "new"
            outputs = [m for m in payloads[-1]["messages"] if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in outputs] == [c[0] for c in calls]
            assert json.loads(outputs[-1]["content"])["lines"][0]["text"] == "new"
        else:
            with pytest.raises(ApprovalDeniedError):
                conversation.send("read, edit and check")
            assert len(payloads) == 1 and observed == ["stateful", "encode", "approve"]
            assert target.read_text() == "old" and not (tmp_path / "ran").exists()
        events = store.load_events(conversation.session.session_id)
        started = [e["payload"]["tool_call_id"] for e in events if e["type"] == "tool_started"]
        assert started == [c[0] for c in (calls if approve else calls[:4])]


def test_parallel_business_failures_and_prechecks_preserve_all_ids(tmp_path):
    (tmp_path / "good.txt").write_text("valid")
    registry = create_tool_registry(tmp_path).with_mode(lambda: ExecutionMode.PLAN)
    calls = [
        call("missing", "read_file", path="missing.txt"),
        ("invalid", "read_file", '{"path":2}'),
        call("good", "read_file", path="good.txt"),
        call("blocked", "run_command", argv=[sys.executable, "-V"]),
        call("last", "file_info", path="good.txt"),
    ]
    with conversation_at(tmp_path, calls, registry) as (conversation, store, payloads):
        assert conversation.send("inspect") == "done"
        outputs = [
            json.loads(m["content"]) for m in payloads[-1]["messages"] if m["role"] == "tool"
        ]
        assert [o["ok"] for o in outputs] == [False, False, True, False, True]
        assert outputs[3]["status"] == "mode_blocked"
        assert not any(
            e["type"] == "command_started"
            for e in store.load_events(conversation.session.session_id)
        )


@pytest.mark.parametrize("failed_event", ["tool_started", "tool_finished"])
def test_parallel_log_failure_cancels_running_read_and_prevents_next_request(
    tmp_path, monkeypatch, failed_event
):
    for name in ["a", "b", "pending"]:
        (tmp_path / f"{name}.txt").write_text(name)
    entered, stopped = Event(), Event()
    readers = []
    read_file = FileTools.read_file

    def read(self, path, args):
        readers.append(current_thread())
        # 开始日志在第二项失败时第一项仍运行；结束日志失败时则让第一项先返回。
        blocked = "a" if failed_event == "tool_started" else "b"
        if path.stem == blocked:
            entered.set()
            try:
                self._cancel.wait(6)
                pytest.fail("read was not cancelled")
            finally:
                stopped.set()
        if failed_event == "tool_finished":
            assert entered.wait(4)
        return read_file(self, path, args)

    monkeypatch.setattr(FileTools, "read_file", read)
    calls = [call(n, "read_file", path=f"{n}.txt") for n in ["a", "b", "pending"]]
    with conversation_at(tmp_path, calls, create_tool_registry(tmp_path)) as (
        conversation,
        store,
        payloads,
    ):
        append = store.append_tool_event

        def fail(sid, kind, data):
            assert get_ident() == coordinator
            failure_id = "b" if failed_event == "tool_started" else "a"
            if kind == failed_event and data["tool_call_id"] == failure_id:
                assert entered.wait(4)
                raise StorageError("simulated log failure")
            return append(sid, kind, data)

        coordinator = get_ident()
        monkeypatch.setattr(store, "append_tool_event", fail)
        with pytest.raises(StorageError, match="simulated"):
            conversation.send("read batch")
        sid = conversation.session.session_id
        assert len(payloads) == 1 and store.load_messages(sid) == []
        assert stopped.is_set() and all(not t.is_alive() for t in readers)
        assert not any(
            e["type"] == "tool_started" and e["payload"]["tool_call_id"] == "pending"
            for e in store.load_events(sid)
        )


def test_cleanup_failure_uses_public_error_and_cannot_dispatch_again(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    live = []

    def read(self, path, args):
        live.append(current_thread())
        entered.set()
        assert release.wait(12)
        return {"lines": []}

    monkeypatch.setattr(FileTools, "read_file", read)
    (tmp_path / "a.txt").write_text("a")
    calls = [call("a", "read_file", path="a.txt"), call("b", "file_info", path=".")]
    with conversation_at(tmp_path, calls, create_tool_registry(tmp_path)) as (
        conversation,
        store,
        payloads,
    ):
        append = store.append_tool_event

        def fail(sid, kind, data):
            if kind == "tool_started" and data["tool_call_id"] == "b":
                assert entered.wait(4)
                raise StorageError("precheck storage failed")
            return append(sid, kind, data)

        monkeypatch.setattr(store, "append_tool_event", fail)
        try:
            with pytest.raises(ToolCleanupError) as raised:
                conversation.send("read")
            assert raised.value.code.value == "tool_cleanup_error"
            assert isinstance(raised.value.__context__, StorageError)
            sid = conversation.session.session_id
            assert store.load_events(sid)[-1]["payload"]["reason"] == "tool_cleanup_error"
            assert len(payloads) == 1
            with pytest.raises(ToolCleanupError):
                conversation.tool_executor.execute(
                    sid,
                    "unused",
                    "unused",
                    [
                        {
                            "id": "later",
                            "function": {"name": "file_info", "arguments": '{"path":"."}'},
                        }
                    ],
                    ToolBudget(3),
                )
        finally:
            release.set()
            for thread in live:
                thread.join(timeout=3)
        assert live and all(not t.is_alive() for t in live)

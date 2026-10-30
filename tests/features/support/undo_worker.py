"""T7.3 独立进程验收入口：真实 HTTP、文件编辑、用户撤销和持锁恢复。"""

import json
import os
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.cli.main import handle_command
from adam.contracts.errors import AgentError, StorageError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools
from tests.features.test_memory_context import config_at


def hold(ready):
    """使用独立标志通知父进程强杀；不让父进程轮询正在写入的会话文件。"""
    ready.write_text("ready", encoding="utf-8")
    threading.Event().wait(30)
    raise RuntimeError("parent did not terminate worker")


class PartialWriter:
    """让真实写入停在半行或半份恢复文件；flush/fsync 后强杀，绕过异常回滚。"""

    def __init__(self, file, ready, marker=None):
        self.file, self.ready, self.marker = file, ready, marker

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()

    def write(self, data):
        if self.marker is None or self.marker in data:
            self.file.write(data[: len(data) // 2])
            self.file.flush()
            os.fsync(self.file.fileno())
            hold(self.ready)
        return self.file.write(data)


def install_fault(store, log_path, phase, ready):
    """只在指定真实 IO 边界暂停；恢复故障必须在 resume 获锁之前安装。"""
    append = store.append_undo_event

    def append_and_hold(session_id, kind, payload):
        append(session_id, kind, payload)
        if kind == phase:
            hold(ready)

    store.append_undo_event = append_and_hold
    if phase == "edit_started":
        append_edit = store.append_edit_event

        def edit_and_hold(session_id, kind, payload):
            append_edit(session_id, kind, payload)
            if kind == "edit_started":
                hold(ready)

        store.append_edit_event = edit_and_hold
    if phase == "after_replace":
        apply = EditTools.apply

        def apply_and_hold(self, plan):
            apply(self, plan)
            hold(ready)

        EditTools.apply = apply_and_hold
    if phase in {"partial_finished", "recovery_write"}:
        original_open = Path.open

        def partial_open(path, mode="r", *args, **kwargs):
            file = original_open(path, mode, *args, **kwargs)
            if phase == "partial_finished" and path == log_path and mode == "r+b":
                return PartialWriter(file, ready, b'"type": "undo_finished"')
            if (
                phase == "recovery_write"
                and path.parent == log_path.parent
                and path.name.endswith(".recovery.tmp")
                and mode == "xb"
            ):
                return PartialWriter(file, ready)
            return file

        Path.open = partial_open
    if phase in {"before_publish", "after_publish"}:
        publish = os.replace

        def replace_and_hold(source, target):
            recovering = Path(target) == log_path and Path(source).name.endswith(".recovery.tmp")
            if recovering and phase == "before_publish":
                hold(ready)
            publish(source, target)
            if recovering and phase == "after_publish":
                hold(ready)

        os.replace = replace_and_hold


def blocked_actions(app):
    """未知副作用下实际尝试撤销、摘要和发送；把公开错误交给父进程核对。"""
    result = {}
    for label, action in (
        ("undo", app.undo),
        ("compact", app.compact),
        ("send", lambda: app.send("unknown result must block model")),
    ):
        try:
            action()
        except StorageError as error:
            result[label] = error.code
        else:
            raise AssertionError(f"{label} unexpectedly continued")
    return result


def main():
    """每次启动创建全新应用；只使用父进程指定的临时数据和本地模型端点。"""
    action, workspace_text, session_id, url, phase, ready_text = sys.argv[1:]
    workspace, ready = Path(workspace_text), Path(ready_text)
    config = config_at(workspace)
    config = replace(config, llm=replace(config.llm, base_url=url))
    approvals, shown = [], []
    result = {"error": None}
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(trust_env=False, timeout=5) as http,
    ):
        if session_id:
            log_path = next(store.root.glob(f"*/*/*/{session_id}.jsonl"))
            install_fault(store, log_path, phase, ready)
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_edit=lambda plan: approvals.append(plan.diff) or True,
        )
        try:
            try:
                if session_id:
                    app.resume(session_id)
                if action == "edit":
                    app.send("edit the requested file")
                elif action == "undo":
                    handle_command("/undo", app, shown.append)
                elif action == "blocked":
                    result["blocked"] = blocked_actions(app)
                elif action != "inspect":
                    raise AssertionError(f"unexpected action {action}")
            except AgentError as error:
                result["error"] = {"code": error.code, "message": str(error)}
            if app.session is not None:
                session_id = app.session.session_id
                log_path = next(store.root.glob(f"*/*/*/{session_id}.jsonl"))
                result.update(
                    session_id=session_id,
                    report=read_report(log_path),
                    history=[message.raw for message in app.history()],
                    usage=asdict(app.usage()),
                    continuation=asdict(app.continuation_status()),
                )
            result.update(approvals=approvals, shown=shown)
        finally:
            app.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""T8.3 独立进程入口；在附件正文、来源和工具结果的真实写入边界暂停。"""

import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.config.settings import ToolLimits
from adam.llm.client import LLMClient
from adam.session.artifacts import ArtifactStore
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at


def hold(ready):
    """仅发布独立同步标志；父进程不读取仍在写入的 JSONL。"""
    ready.write_text("ready", encoding="utf-8")
    threading.Event().wait(30)
    raise RuntimeError("parent did not terminate worker")


class PartialArtifactRecord:
    """实际写出半行并同步磁盘；强杀不会执行 SessionStore 的异常回滚。"""

    def __init__(self, file, ready):
        self.file, self.ready = file, ready

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()

    def write(self, data):
        if b'"type": "artifact_saved"' in data:
            self.file.write(data[: len(data) // 2])
            self.file.flush()
            os.fsync(self.file.fileno())
            hold(self.ready)
        return self.file.write(data)


def install_fault(store, phase, ready, log_path):
    """暂停点只控制时序；正文发布、来源追加和工具结果仍走生产实现。"""
    if phase == "published":
        publish = ArtifactStore._publish

        def published(self, path, data):
            publish(self, path, data)
            hold(ready)

        ArtifactStore._publish = published
    elif phase == "recorded":
        append = store.append_artifact

        def recorded(reference):
            append(reference)
            hold(ready)

        store.append_artifact = recorded
    elif phase == "tool_finished":
        append_tool = store.append_tool_event

        def finished(sid, kind, payload):
            append_tool(sid, kind, payload)
            if kind == "tool_finished":
                hold(ready)

        store.append_tool_event = finished
    elif phase == "partial_record":
        original_open = Path.open

        def partial_open(path, mode="r", *args, **kwargs):
            file = original_open(path, mode, *args, **kwargs)
            if path == log_path and mode == "r+b":
                return PartialArtifactRecord(file, ready)
            return file

        Path.open = partial_open
    else:
        raise AssertionError("unknown boundary")


def main():
    """每次进程重新组装；resume 只读恢复状态，不调用模型、不重跑原命令。"""
    action, workspace_text, sid, url, phase, ready_text = sys.argv[1:]
    workspace, ready = Path(workspace_text), Path(ready_text)
    config = config_at(workspace)
    config = replace(
        config, llm=replace(config.llm, base_url=url), tool_limits=ToolLimits(max_result_chars=1024)
    )
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(trust_env=False, timeout=5) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), workspace, confirm_command=lambda _: True
        )
        try:
            app.resume(sid)
            path = next(store.root.glob(f"*/*/*/{sid}.jsonl"))
            if action == "run":
                install_fault(store, phase, ready, path)
                app.send("read long log once")
                raise AssertionError("worker passed expected pause")
            if action != "resume":
                raise AssertionError("unknown action")
            result = {
                "history": [message.raw for message in app.history()],
                "usage": store.usage(sid),
                "report": read_report(path),
                "references": [
                    item["payload"]
                    for item in store.load_events(sid)
                    if item["type"] == "artifact_saved"
                ],
            }
        finally:
            app.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

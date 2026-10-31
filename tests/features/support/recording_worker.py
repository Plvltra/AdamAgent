"""T9.3 强杀入口：真实 TCP、命令和录制文件；暂停钩子仅用于测试控制时序。"""

import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter
from adam.recording.runtime import CaptureRuntime
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at


def hold(ready):
    """单独发布同步标志，父进程强杀前不打开仍在写入的会话或录制。"""
    ready.write_text("ready", encoding="utf-8")
    threading.Event().wait(30)
    raise RuntimeError("parent did not terminate recording worker")


def install_boundary(phase, ready):
    """仅在真实写入前后暂停；残缺场景实际写半行并同步，不依赖异常回滚模拟崩溃。"""
    original_write = RecordingWriter._write
    tool_call = None

    def write(writer, raw):
        nonlocal tool_call
        record = json.loads(raw)
        if record["type"] == "call_started" and record["data"]["name"] == "tools":
            tool_call = record["data"]["call_id"]
        tool_result = record["type"] == "call_finished" and record["data"]["call_id"] == tool_call
        if phase == "partial_tools" and tool_result:
            writer._file.write(raw[: len(raw) // 2])
            writer._file.flush()
            os.fsync(writer._file.fileno())
            hold(ready)
        if phase == "before_finish" and record["type"] == "recording_finished":
            hold(ready)
        original_write(writer, raw)
        if (phase == "tools_finished" and tool_result) or (
            phase == "recording_finished" and record["type"] == "recording_finished"
        ):
            hold(ready)

    RecordingWriter._write = write


def main():
    """标准应用运行一轮，所有数据在父进程给定的临时目录；不提供生产故障配置。"""
    root_text, url, phase, ready_text = sys.argv[1:]
    root, ready = Path(root_text), Path(ready_text)
    config = config_at(root)
    config = replace(config, llm=replace(config.llm, base_url=url))
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(trust_env=False, timeout=15) as http,
    ):
        sid = store.create_session("interrupted recording", config.system_prompt)
        store.select_session(sid)
        store.append_turn(sid, "old question", "old answer")
        install_boundary(phase, ready)
        with RecordingWriter(
            root / "capture.jsonl", capture_seed(config, store, sid, "task", root)
        ) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            runtime = CaptureRuntime(recorder)
            app = build_application(
                config,
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                root,
                confirm_command=lambda _: True,
                decorate_tools=runtime.tools,
                observe_conversation=runtime.bind,
            )
            try:
                app.resume(sid)
                recorder.run(lambda: app.send("task"))
            finally:
                app.close()
    raise AssertionError("worker passed expected interruption point")


if __name__ == "__main__":
    main()

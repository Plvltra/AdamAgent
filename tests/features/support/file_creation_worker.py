"""B22.3.2独立宿主：在真实发布或撤销边界等待父进程，不伪造执行日志。"""

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response


def run(job):
    """仅MockTransport代替远端；目标、检查点、JSONL和锁都由原生产流程处理。"""
    root, workspace = Path(job["root"]), Path(job["workspace"])
    name, phase = job["name"], job["phase"]
    config = replace(
        config_at(workspace),
        sessions_dir=root / (name + ".sessions"),
        memories_dir=root / (name + ".memory"),
    )
    target = workspace / "new.txt"
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        with (root / (name + ".http")).open("a", encoding="utf-8") as stream:
            stream.write("request\n")
            stream.flush()
            os.fsync(stream.fileno())
        result = (
            tool_response([call("create", "create_file", path="new.txt", content=name * 10000)])
            if len(calls) == 1
            else answer("finished")
        )
        return httpx.Response(200, json=result)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), workspace, confirm_edit=lambda _: True
        )
        sid = app.new("creation fault").session_id

        def pause():
            # 元数据完整写完才发布ready，避免父进程读取半行；释放由测试父进程显式控制。
            (root / (name + ".state")).write_text(json.dumps({"session_id": sid}), encoding="utf-8")
            (root / (name + ".ready")).touch()
            deadline = time.monotonic() + 30
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("parent did not release or kill creation worker")
                time.sleep(0.01)

        real_link, real_unlink, real_event = os.link, os.unlink, store.append_edit_event

        def link(source, destination, *args, **kwargs):
            if Path(destination) == target and phase == "before_link":
                pause()
            real_link(source, destination, *args, **kwargs)
            if Path(destination) == target and phase == "after_link":
                pause()

        def unlink(path, *args, **kwargs):
            if Path(path) == target and phase == "before_unlink":
                pause()
            real_unlink(path, *args, **kwargs)
            if Path(path) == target and phase == "after_unlink":
                pause()

        def event(session, kind, payload):
            real_event(session, kind, payload)
            if kind == "edit_finished" and phase == "after_edit_finished":
                pause()

        os.link, os.unlink, store.append_edit_event = link, unlink, event
        try:
            app.send("create a file")
            if phase in {"before_unlink", "after_unlink"}:
                app.undo()
            return {"session_id": sid, "edits": store.load_edits(sid), "calls": len(calls)}
        finally:
            os.link, os.unlink, store.append_edit_event = real_link, real_unlink, real_event
            app.close()


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(run(job), ensure_ascii=False))

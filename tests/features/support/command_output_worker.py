"""独立宿主接收 Python SIGINT 或命令超时，实际组装原应用并输出持久结果摘要。"""

import _thread
import json
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from adam.config.settings import ArtifactConfig, ToolLimits
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call
from tests.features.test_tool_artifacts import application
from tests.features.test_tool_loop import answer, tool_response

root, mode = Path(sys.argv[1]), sys.argv[2]
stopped = Event()


def interrupt_when_ready():
    """只在两个真实管道均已写出尾部后投递 SIGINT，不中断 pytest 主进程。"""
    deadline = monotonic() + 15
    while not stopped.wait(0.02) and monotonic() < deadline:
        if (root / "ready").exists():
            _thread.interrupt_main()
            return


task = Path(__file__).with_name("command_output_task.py")


def respond(body, phase):
    if phase == 1:
        return tool_response(
            [
                call(
                    "long",
                    "run_command",
                    argv=[sys.executable, str(task)],
                    timeout_seconds=2 if mode == "timeout" else 20,
                )
            ]
        )
    return answer("command stopped")


config = replace(
    config_at(root),
    tool_limits=ToolLimits(max_result_chars=2048),
    artifacts=ArtifactConfig(max_bytes=2000),
)
watcher = Thread(target=interrupt_when_ready) if mode == "interrupt" else None
if watcher:
    watcher.start()
interrupted = False
try:
    with application(root, respond, config=config) as (app, store, requests):
        try:
            app.send("inspect long command")
        except KeyboardInterrupt:
            interrupted = True
        sid = app.session.session_id
        turn_id = next(
            e["payload"]["turn_id"] for e in store.load_events(sid) if e["type"] == "turn_started"
        )
        result = {
            "session_id": sid,
            "interrupted": interrupted,
            "requests": len(requests),
            "executions": store.load_executions(sid),
            # 成功轮次由消息提交派生状态，不要求另有一条 turn_finished 事件。
            "turn_status": store.execution_report(sid, turn_id).status,
        }
finally:
    stopped.set()
    if watcher:
        watcher.join(3)
        assert not watcher.is_alive()
print(json.dumps(result, ensure_ascii=False))

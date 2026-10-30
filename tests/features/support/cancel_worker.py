"""独立进程验证取消；辅助线程投递 Python SIGINT，业务请求、读取与命令都真实执行。"""

import _thread
import json
import sys
import threading
import time
from pathlib import Path

from adam.bootstrap import open_application
from adam.cli.main import chat
from adam.llm.streaming import StreamReply
from adam.session.diagnostics import read_report
from adam.tool_calls.file_tools import FileTools

root = Path(sys.argv[1])
mode = sys.argv[2]
output = []
prompts = []
parallel_workers, stopped_reads, remaining_at_prompt = [], [], []
# 规划中取消后显式切回 act，验证占用已解除；未知用量仍需新建会话。
inputs = iter(
    ["/mode plan", "start", "/mode act", "again", "/new next", "after", "/exit"]
    if mode == "plan_body"
    else ["start", "again", "/new next", "after", "/exit"]
)


def read(prompt):
    prompts.append(prompt)
    if parallel_workers:
        remaining_at_prompt.append([t.name for t in parallel_workers if t.is_alive()])
    if mode == "confirm" and "允许本次" in prompt:
        raise KeyboardInterrupt
    return next(inputs)


def write(text):
    output.append(text)


def cancel_when_ready():
    # 在独立进程投递停止信号，避免 pytest 主进程被意外中断。
    deadline = time.monotonic() + 20
    while not (root / "cancel").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if (root / "cancel").exists():
        _thread.interrupt_main()


original_accept = StreamReply.accept


def observe_usage(self, event):
    """只观察真实解析结果，确保已知用量场景在收到 usage 后才投递取消。"""
    text = original_accept(self, event)
    if self.usage is not None and mode == "known_body":
        (root / "usage_received").touch()
    return text


original_read = FileTools.read_file


def observe_parallel_read(self, path, args):
    """真实文件读取后暂缓返回；由生产调度器传来的组令牌解除等待，便于准确投递 SIGINT。"""
    parallel_workers.append(threading.current_thread())
    result = original_read(self, path, args)
    (root / f"reader-{path.stem}.ready").touch()
    try:
        self._cancel.wait(15)
        return result
    finally:
        stopped_reads.append(path.stem)


if mode == "parallel_reads":
    FileTools.read_file = observe_parallel_read
StreamReply.accept = observe_usage
threading.Thread(target=cancel_when_ready, daemon=True).start()
with open_application(
    root / "config.toml",
    None,
    root,
    confirm_command=(lambda _: read("允许本次")) if mode == "confirm" else (lambda _: True),
) as app:
    chat(app, read=read, write=write, fragment=write)
reports = [read_report(path) for path in sorted((root / "sessions").rglob("*.jsonl"))]
print(
    json.dumps(
        {
            "output": output,
            "prompts": prompts,
            "reports": reports,
            "parallel": {
                "stopped": sorted(stopped_reads),
                "remaining_at_prompt": remaining_at_prompt,
                "live_after_close": [t.name for t in parallel_workers if t.is_alive()],
            },
        },
        ensure_ascii=False,
    )
)

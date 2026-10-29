"""独立进程验证取消；辅助线程投递 Python SIGINT，业务请求和命令都真实执行。"""

import _thread
import json
import sys
import threading
import time
from pathlib import Path

from adam.bootstrap import open_application
from adam.cli import chat
from adam.diagnostics import read_report

root = Path(sys.argv[1])
mode = sys.argv[2]
output = []
prompts = []
inputs = iter(["start", "again", "/new next", "after", "/exit"])


def read(prompt):
    prompts.append(prompt)
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


threading.Thread(target=cancel_when_ready, daemon=True).start()
with open_application(
    root / "config.toml",
    None,
    root,
    confirm_command=(lambda _: read("允许本次")) if mode == "confirm" else (lambda _: True),
) as app:
    chat(app, read=read, write=write, fragment=write)
reports = [read_report(path) for path in sorted((root / "sessions").rglob("*.jsonl"))]
print(json.dumps({"output": output, "prompts": prompts, "reports": reports}, ensure_ascii=False))

"""供强杀验收启动的独立进程；HTTP、Conversation 和 JSONL 均走实际实现。"""

import json
import os
import sys
import threading
from pathlib import Path

import httpx

from adam.config import LLMConfig
from adam.conversation import Conversation
from adam.diagnostics import read_report
from adam.errors import BudgetError
from adam.llm import LLMClient
from adam.storage import SessionStore

mode, root_text, session_id, url, phase, ready_text = sys.argv[1:]
root, ready = Path(root_text), Path(ready_text)
log_path = next(root.glob(f"*/*/*/{session_id}.jsonl"))


def hold():
    """通知父进程到达目标边界；父进程直接 kill，子进程没有机会运行异常收尾。"""
    ready.write_text("ready", encoding="utf-8")
    threading.Event().wait(30)
    raise RuntimeError("parent did not terminate worker")


class PartialWriter:
    """在真实消息追加中只写首行和下一行前缀，flush/fsync 后暂停，制造物理残缺尾行。"""

    def __init__(self, file):
        self.file = file

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()

    def write(self, data):
        if b'"type": "turn_completed"' in data:
            boundary = data.index(b"\n") + 1 + 12
            self.file.write(data[:boundary])
            self.file.flush()
            os.fsync(self.file.fileno())
            hold()
        return self.file.write(data)


if mode == "run" and phase == "message_write":
    original_open = Path.open

    def partial_open(path, mode="r", *args, **kwargs):
        file = original_open(path, mode, *args, **kwargs)
        return PartialWriter(file) if path == log_path and mode == "r+b" else file

    Path.open = partial_open

with (
    SessionStore(root) as store,
    httpx.Client(trust_env=False) as http,
):
    conversation = Conversation(store, LLMClient(LLMConfig(url, "test", "test-only-key"), http), "")
    conversation.resume(session_id)
    if mode == "run":
        conversation.send("new question")
        hold()  # after_commit：真实完整提交已完成，尚未展示回答。
    else:
        blocked = False
        if phase == "request":
            try:
                conversation.send("budget must block")
            except BudgetError:
                blocked = True
        print(
            json.dumps(
                {
                    "report": read_report(log_path),
                    "history": [m.content for m in store.load_messages(session_id)],
                    "blocked": blocked,
                },
                ensure_ascii=False,
            )
        )

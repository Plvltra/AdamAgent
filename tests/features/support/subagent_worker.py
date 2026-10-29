"""独立进程运行真实委派；测试信号与故障停顿不进入生产实现。"""

import _thread
import json
import sys
import threading
import time
from pathlib import Path

from adam.bootstrap import open_application
from adam.cli.main import chat
from adam.llm.streaming import StreamReply
from adam.session.delegation import DelegationStore
from adam.session.diagnostics import read_report

root, mode, tag = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
output = []
original_accept = StreamReply.accept
original_update = DelegationStore.update_run


def observe_usage(self, event):
    """观察真实 SSE 解析后的用量，避免取消测试依赖固定网络睡眠。"""
    text = original_accept(self, event)
    content = self.message.get("content") or ""
    if self.usage is not None and content.startswith("child "):
        (root / ("usage-" + content.split()[-1])).touch()
    return text


def pause_after_child_commit(self, agent_id, run_id, **changes):
    """只在子 JSONL 已成功提交、索引完成标记尚未保存时暂停，等待父进程强杀。"""
    if mode == "committed" and changes.get("status") == "completed":
        (root / ("committed-" + agent_id)).touch()
        while True:
            time.sleep(0.1)
    return original_update(self, agent_id, run_id, **changes)


def interrupt_when_requested():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (root / "cancel").exists():
        time.sleep(0.02)
    if (root / "cancel").exists():
        _thread.interrupt_main()


def tool_started(name):
    if name == "wait_agents":
        (root / "parent-waiting").touch()


StreamReply.accept = observe_usage
DelegationStore.update_run = pause_after_child_commit
if mode == "cancel":
    threading.Thread(target=interrupt_when_requested, daemon=True).start()

with open_application(
    root / "config.toml",
    None,
    root,
    on_notice=output.append,
    on_tool=tool_started,
) as app:
    if mode == "resume":
        app.resume((root / "session-id").read_text(encoding="utf-8"))
    else:
        app.new(tag)
        (root / "session-id").write_text(app.session.session_id, encoding="utf-8")
        inputs = iter(["start " + tag, "/exit"])
        chat(app, read=lambda _: next(inputs), write=output.append, fragment=output.append)
    usage = app.usage()
    session_id = app.session.session_id
    directory = app.sessions_dir / "_subagents" / session_id
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    parent_file = next(app.sessions_dir.glob(f"*/*/*/{session_id}.jsonl"))
    reports = [read_report(parent_file)] + [
        read_report(path) for path in sorted((directory / "sessions").glob("*/*/*/*.jsonl"))
    ]
    result = {
        "output": output,
        "index": index,
        "reports": reports,
        "tokens": usage.tokens,
        "unknown": usage.unknown,
        "history": [message.content for message in app.history()],
    }

result["live_children"] = [
    thread.name for thread in threading.enumerate() if thread.name.startswith("adam-child")
]
print(json.dumps(result, ensure_ascii=False))

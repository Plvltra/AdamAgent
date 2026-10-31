"""B01.3 独立进程：真实 HTTP 与 JSONL，父进程在已落盘提醒/停止结果后强杀。"""

import json
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event

import httpx

from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at


def main():
    """所有数据留在测试目录；恢复阶段只在显式新输入之后允许发请求。"""
    root_text, url, mode, phase = sys.argv[1:]
    root = Path(root_text)
    config = config_at(root)
    config = replace(config, llm=replace(config.llm, base_url=url))
    ready = root / "ready.json"
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(trust_env=False, timeout=10) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), root)
        try:
            if mode == "kill":
                original = store.append_tool_event

                def append(sid, kind, payload):
                    original(sid, kind, payload)
                    if (
                        kind == "tool_finished"
                        and json.loads(payload["output"]).get("no_progress", {}).get("action")
                        == phase
                    ):
                        ready.write_text(json.dumps({"sid": sid}), encoding="utf-8")
                        Event().wait(30)
                        raise AssertionError("parent did not kill worker")

                store.append_tool_event = append
                app.send("old task")
                raise AssertionError("worker passed kill boundary")
            sid = json.loads(ready.read_text(encoding="utf-8"))["sid"]
            app.resume(sid)
            events = store.load_events(sid)
            recovery = [
                event["payload"] for event in events if event["type"] == "session_recovered"
            ]
            before = {
                "recovery": recovery,
                "usage": store.usage(sid),
                "history": len(store.load_messages(sid)),
            }
            assert app.send("new task") == "still unreadable"
            results = [
                json.loads(e["payload"]["output"])
                for e in store.load_events(sid)
                if e["type"] == "tool_finished"
            ]
            print(json.dumps({"before": before, "results": results, "usage": store.usage(sid)}))
        finally:
            app.close()


if __name__ == "__main__":
    main()

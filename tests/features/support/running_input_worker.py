"""B27.3独立宿主：在真实输入落盘、请求或成功历史提交后等待强杀。"""

import json
import sys
import time
from pathlib import Path

from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig
from adam.contracts.models import Completion
from adam.session.storage import SessionStore


def run(root, phase):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        root / "sessions",
        "system",
    )

    def pause():
        path = next((config.sessions_dir / "_inputs").glob("*/*.json"))
        (root / "ready.tmp").write_text(
            json.dumps({"session_id": path.parent.name, "turn_id": path.stem}), encoding="utf-8"
        )
        (root / "ready.tmp").replace(root / "ready.json")
        while True:
            time.sleep(0.1)

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            with (root / "requests").open("a", encoding="utf-8") as stream:
                stream.write("request\n")
            if self.calls == 1:
                app.submit_input("preserve this correction", "correction")
                if phase == "accepted":
                    pause()
            elif phase == "in_context":
                pause()
            return Completion({"role": "assistant", "content": "answer"}, (1, 1))

    with SessionStore(config.sessions_dir) as store:
        app = build_application(config, store, Model(), root, live_input=True)
        if phase == "committed":
            original = store.append_messages

            def commit_then_pause(*args, **kwargs):
                original(*args, **kwargs)
                pause()

            store.append_messages = commit_then_pause
        try:
            app.send("initial")
        finally:
            app.close()


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])

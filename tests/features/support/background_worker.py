"""B26.3独立宿主与真实命令；父测试强杀宿主，命令通过释放文件自行退出。"""

import json
import sys
import time
from pathlib import Path

from adam.application.background import BackgroundService
from adam.config.settings import ToolLimits
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.command_tools import CommandTools


def run(root: Path, child: bool) -> None:
    """ready只在真实输出已经持久保存后发布；子命令有硬性自退出期限防止遗留。"""
    if child:
        with (root / "launches").open("a", encoding="utf-8") as stream:
            stream.write("started\n")
        print("observed-before-crash", flush=True)
        deadline = time.monotonic() + 10
        while not (root / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        (root / "child-done").touch()
        return
    service = BackgroundService(
        root / "tasks", BackgroundJobs(CommandTools(root, ToolLimits(), lambda _: True))
    )
    try:
        task = service.start(
            "1234abcd",
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "tests.features.support.background_worker",
                    str(root),
                    "child",
                ],
                "cwd": str(Path.cwd()),
            },
            {"turn_id": "turn", "request_id": "request", "tool_call_id": "call"},
        )["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.query("1234abcd", task)
            if "observed-before-crash" in result["stdout"]["text"]:
                # query先保存快照，随后才允许测试终止宿主。
                (root / "ready.tmp").write_text(json.dumps({"task_id": task}), encoding="utf-8")
                (root / "ready.tmp").replace(root / "ready.json")
                while True:
                    time.sleep(0.1)
            time.sleep(0.02)
        raise RuntimeError("child output did not arrive")
    finally:
        service.close()


if __name__ == "__main__":
    run(Path(sys.argv[1]), len(sys.argv) > 2)

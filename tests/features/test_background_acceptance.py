"""B26.3：双进程交错与独立宿主强杀；不伪造终态或替代操作系统文件锁。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adam.application.background import BackgroundService
from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.command_tools import CommandTools


def test_two_live_processes_keep_outputs_separate_and_cancel_independently(tmp_path):
    service = BackgroundService(
        tmp_path / "tasks", BackgroundJobs(CommandTools(tmp_path, ToolLimits(), lambda _: True))
    )
    owner = "1234abcd"
    try:
        tasks = {}
        for label in ("a", "b"):
            script = "import sys,time; from pathlib import Path; label=sys.argv[1]; print(label+'-out',flush=True); print(label+'-err',file=sys.stderr,flush=True); deadline=time.monotonic()+8\nwhile not Path(label+'.release').exists() and time.monotonic()<deadline: time.sleep(.02)\nprint(label+'-done',flush=True)"
            tasks[label] = service.start(
                owner, {"argv": [sys.executable, "-c", script, label]}, {}
            )["task_id"]
        deadline = time.monotonic() + 3
        observations = {}
        while time.monotonic() < deadline:
            observations = {label: service.query(owner, task) for label, task in tasks.items()}
            if all(
                row["stdout"]["text"] and row["stderr"]["text"] for row in observations.values()
            ):
                break
            time.sleep(0.01)
        for label, row in observations.items():
            assert row["status"] == "running"
            assert row["stdout"]["text"].strip() == label + "-out"
            assert row["stderr"]["text"].strip() == label + "-err"
        with pytest.raises(ToolError, match="不属于"):
            service.query("8765abcd", tasks["a"], action="cancel")
        cancelled = service.query(owner, tasks["a"], action="cancel", seconds=3)
        assert cancelled["status"] == "cancelled"
        assert service.query(owner, tasks["b"])["status"] == "running"
        (tmp_path / "b.release").touch()
        result = service.query(
            owner,
            tasks["b"],
            action="wait",
            seconds=3,
            stdout_cursor=observations["b"]["stdout"]["next_cursor"],
        )
        assert result["status"] == "completed" and result["exit_code"] == 0
        assert result["stdout"]["text"].strip() == "b-done"
    finally:
        service.close()


def test_killed_host_recovers_real_task_without_restarting_or_claiming_exit(tmp_path):
    host = subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.background_worker", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    reader = BackgroundService(
        tmp_path / "tasks", BackgroundJobs(CommandTools(tmp_path, ToolLimits()))
    )
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 6
        while not ready.exists() and host.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "独立宿主未完成真实启动与输出观察"
        task = json.loads(ready.read_text(encoding="utf-8"))["task_id"]
        with pytest.raises(ToolError, match="另一个终端"):
            reader.query("1234abcd", task)
        host.kill()
        host.wait(timeout=5)
        result = reader.query("1234abcd", task)
        assert result["status"] == "interrupted" and result["exit_code"] is None
        assert "observed-before-crash" in result["stdout"]
        assert result["source"]["tool_call_id"] == "call"
        assert reader.query("1234abcd", task)["status"] == "interrupted"
        assert (tmp_path / "launches").read_text(encoding="utf-8").splitlines() == ["started"]
    finally:
        (tmp_path / "release").touch()
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)
        host.communicate(timeout=5)
        reader.close()
        deadline = time.monotonic() + 5
        while (
            (tmp_path / "launches").exists()
            and not (tmp_path / "child-done").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if (tmp_path / "launches").exists():
            assert (tmp_path / "child-done").exists(), "测试命令必须按释放信号自行退出"

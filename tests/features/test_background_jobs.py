"""B26.1：真实后台进程、一次批准、有界输出和会话所有权。"""

import sys

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.command_tools import CommandTools


def test_start_returns_before_exit_and_queries_do_not_reapprove(tmp_path):
    approvals = []
    commands = CommandTools(
        tmp_path, ToolLimits(), lambda command: approvals.append(command) or True
    )
    jobs = BackgroundJobs(commands)
    try:
        task = jobs.start(
            "session-a",
            {"argv": [sys.executable, "-c", "import time; time.sleep(.3); print('done')"]},
        )
        assert task["status"] == "running"
        assert jobs.snapshot("session-a", task["task_id"])["status"] == "running"
        with pytest.raises(ToolError, match="不属于"):
            jobs.snapshot("session-b", task["task_id"])
        result = jobs.wait("session-a", task["task_id"], 3)
        assert result["status"] == "completed" and result["exit_code"] == 0
        assert result["stdout"].strip() == "done" and len(approvals) == 1
    finally:
        jobs.close()


def test_denial_no_side_effect_and_output_is_bounded(tmp_path):
    denied = BackgroundJobs(CommandTools(tmp_path, ToolLimits()))
    assert (
        denied.start("a", {"argv": [sys.executable, "-c", "raise Exception()"]})["status"]
        == "denied"
    )
    denied.close()
    jobs = BackgroundJobs(CommandTools(tmp_path, ToolLimits(), lambda _: True, capture_bytes=64))
    try:
        task = jobs.start(
            "a", {"argv": [sys.executable, "-c", "import sys; print('x'*10000); sys.exit(7)"]}
        )
        result = jobs.wait("a", task["task_id"], 3)
        assert result["status"] == "completed" and result["exit_code"] == 7
        assert result["truncated"] and len(result["stdout"]) < 150
        assert result["output_capture"]["stdout"]["omitted_bytes"] > 9000
    finally:
        jobs.close()


def test_cancel_and_timeout_reap_process_and_release_capacity(tmp_path):
    jobs = BackgroundJobs(CommandTools(tmp_path, ToolLimits(), lambda _: True), max_running=1)
    args = {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    try:
        first = jobs.start("a", args)["task_id"]
        with pytest.raises(ToolError, match="数量上限"):
            jobs.start("a", args)
        jobs.cancel("a", first)
        assert jobs.wait("a", first, 8)["status"] == "cancelled"
        second = jobs.start("a", {**args, "timeout_seconds": 1})["task_id"]
        result = jobs.wait("a", second, 8)
        assert result["status"] == "timed_out" and result["exit_code"] is not None
    finally:
        jobs.close()

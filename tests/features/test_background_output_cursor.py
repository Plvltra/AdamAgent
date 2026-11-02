"""后台输出续读：真实进程未结束时可读，缺口可见且查询不触发执行。"""

import io
import sys
import time

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.command_output import OutputCapture
from adam.tool_calls.command_tools import CommandTools


def test_live_output_can_be_read_before_process_exit(tmp_path):
    approvals = []
    jobs = BackgroundJobs(
        CommandTools(tmp_path, ToolLimits(), lambda cmd: approvals.append(cmd) or True)
    )
    try:
        task = jobs.start(
            "a",
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(30)",
                ]
            },
        )["task_id"]
        deadline = time.monotonic() + 3
        while True:
            page = jobs.read_output("a", task)
            if page["stdout"]["text"] or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert page["status"] == "running"
        assert page["stdout"]["text"].strip() == "ready"
        assert not page["stdout"]["eof"]
        assert (
            jobs.read_output("a", task, stdout_cursor=page["stdout"]["next_cursor"])["stdout"][
                "text"
            ]
            == ""
        )
        with pytest.raises(ToolError, match="不属于"):
            jobs.read_output("b", task)
        with pytest.raises(ToolError, match="cursor"):
            jobs.read_output("a", task, stdout_cursor=1000)
        assert len(approvals) == 1
    finally:
        jobs.close()


def test_cursor_reports_gap_and_preserves_utf8_pages():
    capture = OutputCapture(16)
    capture.drain(io.BytesIO(b"HEAD1234" + b"x" * 100 + "结束".encode() + b"!!"))
    first = capture.read_since(0, 8)
    assert first["text"] == "HEAD1234"
    tail = capture.read_since(first["next_cursor"], 4)
    assert tail["text"] == "结" and tail["dropped_bytes"] == 100
    last = capture.read_since(tail["next_cursor"], 8)
    assert last["text"] == "束!!" and not last["has_more"]
    assert capture.read_since(last["next_cursor"])["text"] == ""


def test_cursor_handles_character_cut_by_capture_boundary():
    capture = OutputCapture(9)
    capture.drain(io.BytesIO("你好世界".encode()))
    first = capture.read_since()
    assert first["text"] == "你" and first["dropped_bytes"] == 2
    last = capture.read_since(first["next_cursor"])
    assert last["text"] == "界" and last["dropped_bytes"] == 4
    assert not last["has_more"]
    with pytest.raises(ValueError):
        capture.read_since(True)

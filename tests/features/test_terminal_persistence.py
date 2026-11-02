"""B30.2.1：真实终端输入来源、关闭恢复以及启动前保存故障。"""

import json
import sys
import time

import pytest

from adam.application.terminals import TerminalService
from adam.config.settings import ToolLimits
from adam.contracts.errors import CommandLogError, ToolError
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.terminal_sessions import TerminalSessions


def make_service(tmp_path, confirm=None):
    return TerminalService(
        tmp_path / "records", TerminalSessions(CommandTools(tmp_path, ToolLimits()), confirm)
    )


def test_terminal_input_source_and_closed_snapshot_survive_restart(tmp_path):
    service = make_service(tmp_path, lambda _: True)
    owner = "1234abcd"
    source = {"turn_id": "turn", "request_id": "request", "tool_call_id": "call"}
    try:
        identity = service.start(
            owner,
            {
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "import sys; print(sys.stdin.read(),flush=True)",
                ]
            },
            source,
        )["terminal_id"]
        response = service.send(owner, identity, "persistent input", source, eof=True)
        assert response["input_status"] == "accepted"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.query(owner, identity)
            if result["status"] == "completed":
                break
            time.sleep(0.01)
        assert result["exit_code"] == 0
        assert "persistent input" in result["stdout"]["text"]
        record = json.loads(
            (tmp_path / "records" / owner / (identity + ".json")).read_text(encoding="utf-8")
        )
        assert record["source"] == source
        assert record["inputs"][0]["source"] == source
        assert record["inputs"][0]["status"] == "closed"
    finally:
        service.close()
    restored = make_service(tmp_path)
    try:
        result = restored.query(owner, identity)
        assert result["restored"] and result["status"] == "completed"
        assert result["observation"]["exit_code"] == 0
        with pytest.raises(ToolError, match="不可重连"):
            restored.send(owner, identity, "never replay", source)
        assert restored.stop(owner, identity)["restored"]
    finally:
        restored.close()


def test_save_failure_prevents_terminal_start(tmp_path, monkeypatch):
    approvals = []
    service = make_service(tmp_path, lambda request: approvals.append(request) or True)

    def fail(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr("adam.application.terminals.os.replace", fail)
    try:
        with pytest.raises(CommandLogError):
            service.start("1234abcd", {"argv": [sys.executable, "-c", "print('not started')"]}, {})
        assert approvals == []
    finally:
        service.close()

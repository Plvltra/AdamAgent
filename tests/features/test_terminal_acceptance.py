"""B30.3：真实shell状态、异常退出及宿主强杀后的输入未知恢复。"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.terminal_sessions import TerminalSessions
from tests.features.test_terminal_persistence import make_service
from tests.features.test_terminal_sessions import wait_output


def test_real_shell_keeps_directory_environment_and_other_shell_isolated(tmp_path):
    (tmp_path / "child").mkdir()
    shell = (
        [shutil.which("powershell.exe"), "-NoLogo", "-NoProfile", "-Command", "-"]
        if os.name == "nt"
        else ["/bin/sh"]
    )
    assert shell[0]
    terminals = TerminalSessions(CommandTools(tmp_path, ToolLimits()), lambda _: True)
    try:
        a, b = [
            terminals.start("owner", {"argv": shell, "env": {"TAG": tag}})["terminal_id"]
            for tag in ("A", "B")
        ]
        change = (
            "Set-Location child; $env:TAG='changed'; [Console]::WriteLine('READY')\n"
            if os.name == "nt"
            else "cd child; export TAG=changed; echo READY\n"
        )
        inspect = (
            "[Console]::WriteLine((Get-Location).Path + '|' + $env:TAG)\n"
            if os.name == "nt"
            else 'printf \'%s|%s\\n\' "$PWD" "$TAG"\n'
        )
        terminals.send("owner", a, change)
        wait_output(terminals, "owner", a, "READY")
        terminals.send("owner", a, inspect)
        wait_output(terminals, "owner", a, str(tmp_path / "child") + "|changed")
        terminals.send("owner", b, inspect)
        wait_output(terminals, "owner", b, str(tmp_path) + "|B")
        terminals.send("owner", a, "exit 7\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = terminals.read("owner", a)
            if result["status"] != "running":
                break
            time.sleep(0.01)
        assert result["status"] == "completed" and result["exit_code"] == 7
        with pytest.raises(ToolError, match="关闭"):
            terminals.send("owner", a, "must not run\n")
        assert terminals.read("owner", b)["status"] == "running"
    finally:
        terminals.close()


def test_host_kill_keeps_input_unknown_without_resending(tmp_path):
    host = subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.terminal_worker", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    reader = make_service(tmp_path)
    try:
        deadline = time.monotonic() + 8
        while (
            not (tmp_path / "ready.json").exists()
            and host.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (tmp_path / "ready.json").exists()
        identity = json.loads((tmp_path / "ready.json").read_text())["terminal_id"]
        with pytest.raises(ToolError, match="另一宿主"):
            reader.query("1234abcd", identity)
        host.kill()
        host.wait(timeout=5)
        snapshot = reader.query("1234abcd", identity)
        assert snapshot["restored"] and snapshot["status"] == "interrupted"
        record = json.loads(
            (tmp_path / "records" / "1234abcd" / (identity + ".json")).read_text(encoding="utf-8")
        )
        assert record["inputs"][0]["status"] == "unknown"
        assert record["inputs"][0]["source"]["tool_call_id"] == "input"
        with pytest.raises(ToolError, match="不可重连"):
            reader.send("1234abcd", identity, "second\n", {})
        assert (tmp_path / "executed").read_text() == "once\n"
        assert reader.query("1234abcd", identity) == snapshot
    finally:
        (tmp_path / "release").touch()
        if host.poll() is None:
            host.kill()
        host.communicate(timeout=5)
        reader.close()
        deadline = time.monotonic() + 5
        while (
            (tmp_path / "executed").exists()
            and not (tmp_path / "child-done").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if (tmp_path / "executed").exists():
            assert (tmp_path / "child-done").exists()

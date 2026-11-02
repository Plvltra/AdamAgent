"""B30.1：真实持久进程的目录/环境隔离及逐次stdin批准。"""

import sys
import time

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.terminal_sessions import TerminalSessions


def wait_output(terminals, owner, identity, marker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = terminals.read(owner, identity)
        if marker in result["stdout"]["text"]:
            return result
        time.sleep(0.01)
    pytest.fail(f"terminal did not produce {marker}: {result}")


def test_live_terminal_keeps_cwd_env_and_separates_other_terminal(tmp_path):
    (tmp_path / "child").mkdir()
    approvals = []

    def approve(request):
        approvals.append(request)
        return "DENIED" not in request.text

    terminals = TerminalSessions(CommandTools(tmp_path, ToolLimits()), approve)
    script = (
        "import os,sys; print('START:'+os.environ['TAG'],flush=True)\n"
        "for line in sys.stdin:\n exec(line)\n"
    )
    try:
        a, b = [
            terminals.start(
                "owner", {"argv": [sys.executable, "-u", "-c", script], "env": {"TAG": tag}}
            )["terminal_id"]
            for tag in ("A", "B")
        ]
        assert a != b
        wait_output(terminals, "owner", a, "START:A")
        wait_output(terminals, "owner", b, "START:B")
        terminals.send(
            "owner",
            a,
            "os.chdir('child'); os.environ['TAG']='changed'; print('changed',flush=True)\n",
        )
        wait_output(terminals, "owner", a, "changed")
        terminals.send(
            "owner",
            a,
            "print('STATE:'+os.path.basename(os.getcwd())+':'+os.environ['TAG'],flush=True)\n",
        )
        wait_output(terminals, "owner", a, "STATE:child:changed")
        assert terminals.send("owner", a, "print('DENIED')\n")["status"] == "denied"
        terminals.send(
            "owner",
            b,
            "print('STATE:'+os.path.basename(os.getcwd())+':'+os.environ['TAG'],flush=True)\n",
        )
        other = wait_output(terminals, "owner", b, "STATE:" + tmp_path.name + ":B")
        assert "changed" not in other["stdout"]["text"]
        with pytest.raises(ToolError, match="不属于"):
            terminals.send("other-owner", a, "print('must not run')\n")
        assert approvals[0].env == (("TAG", "A"),)
        assert approvals[1].env == (("TAG", "B"),)
        terminals.stop("owner", a)
        assert terminals.read("owner", a)["status"] == "cancelled"
        assert terminals.read("owner", b)["status"] == "running"
        assert "DENIED" not in terminals.read("owner", a)["stdout"]["text"]
    finally:
        terminals.close()


def test_default_denial_does_not_start_terminal(tmp_path):
    terminals = TerminalSessions(CommandTools(tmp_path, ToolLimits()))
    try:
        result = terminals.start("owner", {"argv": [sys.executable, "-c", "raise SystemExit(1)"]})
        assert result == {"ok": False, "status": "denied"}
    finally:
        terminals.close()

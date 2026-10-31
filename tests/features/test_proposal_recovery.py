"""T11.3：真实主子进程强杀后恢复候选与编辑事实，不自动继续副作用。"""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


class ProposalProcesses:
    """每个动作都创建新应用进程；只在进程退出后读取业务文件。"""

    def __init__(self, workspace, url, requests):
        self.workspace, self.url, self.requests = workspace, url, requests
        self.target = workspace / "target.py"
        self.original = b"\xef\xbb\xbfvalue = 1\r\n# tail"
        self.changed = self.original.replace(b"value = 1", b"value = 2")
        self.target.write_bytes(self.original)
        self.project = Path(__file__).resolve().parents[2]
        self.worker = Path(__file__).parent / "support" / "proposal_worker.py"
        self.options = {
            "cwd": self.project,
            "env": {
                **os.environ,
                "PYTHONPATH": str(self.project),
                "PYTHONIOENCODING": "utf-8",
            },
            "text": True,
            "encoding": "utf-8",
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }

    def run(self, action, session_id):
        process = subprocess.run(
            self._args(action, session_id, "-"),
            capture_output=True,
            timeout=25,
            **self.options,
        )
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        assert result["live_children"] == []
        return result

    @contextmanager
    def paused(self, phase):
        """子边界还需等主 wait 工具落盘，避免误杀在主模型用量尚未结算的时刻。"""
        ready = self.workspace / "fault-ready"
        waiting = self.workspace / "parent-waiting"
        process = subprocess.Popen(
            self._args("run", "", phase),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **self.options,
        )
        try:
            deadline = time.monotonic() + 20
            while not (ready.exists() and (not phase.startswith("child_") or waiting.exists())):
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=3)
                    pytest.fail(f"worker exited before {phase}: {stdout}\n{stderr}")
                if time.monotonic() >= deadline:
                    pytest.fail(f"worker did not reach {phase}")
                threading.Event().wait(0.02)
            yield process
        finally:
            if process.poll() is None:
                self.kill(process)

    @staticmethod
    def kill(process):
        """操作系统终止真实工作进程，绕过 Python finally 和正常取消收尾。"""
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0

    def _args(self, action, session_id, phase):
        return [
            sys.executable,
            str(self.worker),
            action,
            str(self.workspace),
            session_id,
            self.url,
            phase,
            str(self.workspace / "fault-ready"),
        ]


def model_response(body, target):
    """从本次用户输入后的实际工具结果路由，主子请求交错不影响脚本。"""
    start = max(i for i, message in enumerate(body["messages"]) if message["role"] == "user")
    current = {"messages": body["messages"][start:]}
    seen = outputs(current)
    user = current["messages"][0]["content"]
    names = {item["function"]["name"] for item in body["tools"]}
    if user.startswith("apply saved "):
        if not seen:
            return tool_response([call("retry", "apply_proposal", **json.loads(user[12:]))])
        return answer("explicit application checked")
    if "submit_proposal" in names:
        if not seen:
            return tool_response([call("read", "read_file", path=str(target))])
        if len(seen) == 1:
            assert seen[-1]["ok"] and seen[-1]["version"]
            return tool_response(
                [
                    call(
                        "submit",
                        "submit_proposal",
                        read_call_id="read",
                        old_text="value = 1",
                        new_text="value = 2",
                        reason="correct value",
                    )
                ]
            )
        assert seen[-1]["status"] == "submitted"
        return answer("candidate saved")
    if not seen:
        return tool_response(
            [call("spawn", "spawn_agent", name="helper", task="propose correction")]
        )
    if len(seen) == 1:
        return tool_response([call("wait", "wait_agents", agent_ids=[seen[0]["agent_id"]])])
    if len(seen) == 2:
        assert seen[-1]["agents"][0]["status"] == "completed"
        return tool_response(
            [
                call(
                    "collect",
                    "collect_proposals",
                    agent_id=seen[0]["agent_id"],
                    run_id=seen[0]["run_id"],
                )
            ]
        )
    if len(seen) == 3:
        return tool_response(
            [
                call(
                    "apply",
                    "apply_proposal",
                    **{key: seen[-1][key] for key in ("agent_id", "run_id", "proposal_id", "path")},
                )
            ]
        )
    assert seen[-1]["status"] == "applied"
    return answer("proposal applied")


@pytest.fixture
def proposal_processes(tmp_path):
    """仅模型响应为本地 TCP 假服务；子线程、文件、检查点和日志均真实运行。"""
    requests, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            try:
                result = model_response(body, tmp_path / "target.py")
            except Exception as error:
                errors.append(repr(error))
                self.send_error(500)
                return
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield ProposalProcesses(tmp_path, f"http://127.0.0.1:{server.server_port}", requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert errors == []


@pytest.mark.parametrize(
    ("phase", "requests", "child_complete", "edit_status", "changed"),
    [
        ("child_partial", 4, False, None, False),
        ("child_saved", 4, False, None, False),
        ("child_committed", 5, True, None, False),
        ("edit_planned", 7, True, "not_started", False),
        ("edit_started", 7, True, "unknown", False),
        ("after_replace", 7, True, "unknown", True),
        ("edit_finished", 7, True, "applied", True),
        ("turn_completed", 8, True, "applied", True),
    ],
)
def test_strong_termination_restores_only_proven_proposal_and_edit_facts(
    proposal_processes,
    phase,
    requests,
    child_complete,
    edit_status,
    changed,
):
    runtime = proposal_processes
    with runtime.paused(phase) as process:
        runtime.kill(process)
    sid = (runtime.workspace / "session-id").read_text(encoding="utf-8")
    logs = list((runtime.workspace / "sessions").rglob("*.jsonl"))
    original_logs = {path: path.read_bytes() for path in logs}
    assert len(logs) == 2
    assert sum(not raw.endswith(b"\n") for raw in original_logs.values()) == (
        phase == "child_partial"
    )
    expected = runtime.changed if changed else runtime.original
    assert runtime.target.read_bytes() == expected
    result = runtime.run("inspect", sid)
    assert result["error"] is None and result["approvals"] == []
    assert len(runtime.requests) == requests
    assert result["usage"]["tokens"] == requests * 12 and not result["usage"]["unknown"]
    assert result["continuation"]["allowed"] is (edit_status != "unknown")
    turn = result["report"]["turns"][0]
    assert turn["status"] == ("succeeded" if phase == "turn_completed" else "interrupted")
    assert all(item["status"] == "succeeded" for item in turn["requests"])
    assert bool(result["history"]) is (phase == "turn_completed")
    (child,) = result["children"]
    assert child["agent"]["runs"][0]["status"] == ("completed" if child_complete else "interrupted")
    assert child["report"]["turns"][0]["status"] == (
        "succeeded" if child_complete else "interrupted"
    )
    assert bool(child["history"]) is child_complete
    assert child["edits"] == []
    assert len(child["proposals"]) == (0 if phase == "child_partial" else 1)
    assert [edit["status"] for edit in result["edits"]] == (
        [] if edit_status is None else [edit_status]
    )
    if result["edits"]:
        assert result["edits"][0]["proposal"] == child["proposals"][0]
    if phase == "child_partial":
        child_path = next(path for path in logs if "_subagents" in path.parts)
        backups = list(child_path.parent.glob("*.bak"))
        assert [path.read_bytes() for path in backups] == [original_logs[child_path]]
        (repair,) = child["report"]["recoveries"]
        assert repair["payload"]["tail_repaired"]
    stable = {path: path.read_bytes() for path in logs}
    again = runtime.run("inspect", sid)
    assert again == result
    assert {path: path.read_bytes() for path in logs} == stable
    assert runtime.target.read_bytes() == expected and len(runtime.requests) == requests

    if edit_status == "unknown":
        blocked = runtime.run("blocked", sid)
        assert blocked["blocked"] == dict.fromkeys(("undo", "compact", "send"), "execution_unknown")
        assert blocked["approvals"] == [] and blocked["edits"] == result["edits"]
        assert runtime.target.read_bytes() == expected and len(runtime.requests) == requests
    elif phase in {"child_saved", "child_committed", "edit_planned"}:
        # 只有新的用户输入才尝试应用；未提交成功子轮即使有候选，也不能升级为可用结果。
        applied = runtime.run("apply", sid)
        allowed = child_complete
        assert applied["error"] is None and len(applied["approvals"]) == int(allowed)
        last = applied["report"]["turns"][-1]
        (tool,) = last["tools"]
        assert json.loads(tool["output"])["ok"] is allowed
        assert runtime.target.read_bytes() == (runtime.changed if allowed else runtime.original)
        assert len(runtime.requests) == requests + 2
        if allowed:
            assert applied["edits"][-1]["status"] == "applied"
            assert applied["edits"][-1]["proposal"] == child["proposals"][0]
    elif edit_status == "applied":
        undone = runtime.run("undo", sid)
        assert undone["error"] is None and len(undone["approvals"]) == 1
        assert undone["history"] == result["history"] and undone["edits"] == result["edits"]
        assert undone["report"]["undos"][-1]["status"] == "applied"
        assert runtime.target.read_bytes() == runtime.original and len(runtime.requests) == requests

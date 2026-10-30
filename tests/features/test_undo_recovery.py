"""T7.3：真实进程编辑、重启、用户撤销与强杀恢复；模型仅由本地 TCP 服务模拟。"""

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


class UndoProcesses:
    """每次操作使用全新应用进程；父进程只在退出后读取业务日志。"""

    def __init__(self, workspace, url, requests, control):
        self.workspace, self.url = workspace, url
        self.requests, self.control = requests, control
        self.source = workspace / "source.txt"
        self.original = b"\xef\xbb\xbfold\r\n" + "尾行".encode()
        self.source.write_bytes(self.original)
        self.project = Path(__file__).resolve().parents[2]
        self.worker = Path(__file__).parent / "support" / "undo_worker.py"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.project),
            "PYTHONIOENCODING": "utf-8",
        }
        self.options = {
            "cwd": self.project,
            "env": self.env,
            "text": True,
            "encoding": "utf-8",
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        self.sequence = 0

    def log_path(self, session_id):
        return next((self.workspace / "sessions").glob(f"*/*/*/{session_id}.jsonl"))

    def run(self, action, session_id=""):
        """完成一次独立进程操作；业务拒绝在 JSON 中返回，进程故障直接使测试失败。"""
        process = subprocess.run(
            self._args(action, session_id, "-", self.workspace / "unused-ready"),
            capture_output=True,
            timeout=20,
            **self.options,
        )
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    @contextmanager
    def paused(self, action, session_id, phase):
        """等候明确边界并保证回收；不读取正在写入的 JSONL，避免 Windows 替换竞争。"""
        self.sequence += 1
        ready = self.workspace / f"ready-{self.sequence}"
        process = subprocess.Popen(
            self._args(action, session_id, phase, ready),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **self.options,
        )
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
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
        """真实终止进程，不执行 Python 的异常处理和 finally。"""
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0

    def _args(self, action, session_id, phase, ready):
        return [
            sys.executable,
            str(self.worker),
            action,
            str(self.workspace),
            session_id,
            self.url,
            phase,
            str(ready),
        ]


@pytest.fixture
def undo_processes(tmp_path):
    """三次真实 HTTP 完成读文件、按版本修改、回答；后续本地撤销不应再请求此服务。"""
    requests = []
    control = {"path": "source.txt", "old": "old", "new": "new"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            phase = (len(requests) - 1) % 3
            if phase == 0:
                result = tool_response([call("read", "read_file", path=control["path"])])
            elif phase == 1:
                result = tool_response(
                    [
                        call(
                            "edit",
                            "replace_text",
                            path=control["path"],
                            old_text=control["old"],
                            new_text=control["new"],
                            expected_version=outputs(body)[-1]["version"],
                        )
                    ]
                )
            else:
                assert outputs(body)[-1]["status"] == "applied"
                result = answer("edited")
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield UndoProcesses(tmp_path, f"http://127.0.0.1:{server.server_port}", requests, control)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def assert_preserved(result, edited):
    """比较完整已提交消息、原编辑事实和消费；撤销不能偷偷改写过去的对话。"""
    assert result["error"] is None
    assert result["history"] == edited["history"]
    assert result["usage"] == edited["usage"]
    assert result["report"]["turns"][0] == edited["report"]["turns"][0]


def test_edit_exit_restart_then_cli_undo_restores_bytes_and_keeps_evidence(undo_processes):
    runtime = undo_processes
    edited = runtime.run("edit")
    sid = edited["session_id"]
    assert edited["error"] is None and len(edited["approvals"]) == 1
    assert runtime.source.read_bytes() == runtime.original.replace(b"old", b"new")
    reopened = runtime.run("inspect", sid)
    assert_preserved(reopened, edited)
    assert reopened["report"]["undos"] == [] and reopened["approvals"] == []
    undone = runtime.run("undo", sid)
    assert_preserved(undone, edited)
    assert runtime.source.read_bytes() == runtime.original
    assert len(undone["approvals"]) == 1
    assert "-new" in undone["approvals"][0] and "+old" in undone["approvals"][0]
    assert any("已撤销文件修改" in line for line in undone["shown"])
    undo = undone["report"]["undos"][0]
    edit = edited["report"]["turns"][0]["edits"][0]
    assert undo["status"] == "applied" and undo["edit_id"] == edit["edit_id"]
    assert undo["after_version"] == edit["before_version"] and undo["duration_ms"] >= 0
    for name in ("turn_id", "request_id", "tool_call_id"):
        assert undo[f"source_{name}"] == edit[name]
    raw = runtime.log_path(sid).read_bytes()
    again = runtime.run("inspect", sid)
    assert again["report"] == undone["report"] and again["approvals"] == []
    assert runtime.log_path(sid).read_bytes() == raw and len(runtime.requests) == 3


@pytest.mark.parametrize(
    ("phase", "status", "restored"),
    [
        ("undo_requested", "interrupted", False),
        ("undo_prepared", "interrupted", False),
        ("undo_started", "unknown", False),
        ("after_replace", "unknown", True),
        ("partial_finished", "unknown", True),
        ("undo_finished", "applied", True),
    ],
)
def test_killed_undo_recovers_only_known_facts(undo_processes, phase, status, restored):
    runtime = undo_processes
    edited = runtime.run("edit")
    sid = edited["session_id"]
    path = runtime.log_path(sid)
    with runtime.paused("undo", sid, phase) as process:
        runtime.kill(process)
    interrupted = path.read_bytes()
    assert interrupted.endswith(b"\n") is (phase != "partial_finished")
    expected = runtime.original if restored else runtime.original.replace(b"old", b"new")
    assert runtime.source.read_bytes() == expected
    reopened = runtime.run("inspect", sid)
    assert_preserved(reopened, edited)
    assert reopened["approvals"] == [] and runtime.source.read_bytes() == expected
    undo = reopened["report"]["undos"][0]
    assert undo["status"] == status
    expected_version = edited["report"]["turns"][0]["edits"][0]["before_version"]
    assert undo["after_version"] == (expected_version if status == "applied" else None)
    assert (undo["duration_ms"] is None) is (status != "applied")
    repairs = reopened["report"]["recoveries"]
    backups = list(path.parent.glob("*.bak"))
    if status == "interrupted" or phase == "partial_finished":
        assert [backup.read_bytes() for backup in backups] == [interrupted]
        assert len(repairs) == 1
        assert repairs[0]["payload"]["tail_repaired"] is (phase == "partial_finished")
        assert repairs[0]["payload"].get("undo_ids", []) == (
            [undo["undo_id"]] if status == "interrupted" else []
        )
    else:
        assert repairs == [] and backups == [] and path.read_bytes() == interrupted
    stable = path.read_bytes()
    again = runtime.run("inspect", sid)
    assert again["report"] == reopened["report"] and path.read_bytes() == stable
    if status == "unknown":
        blocked = runtime.run("blocked", sid)
        assert_preserved(blocked, edited)
        assert blocked["blocked"] == dict.fromkeys(("undo", "compact", "send"), "execution_unknown")
        assert blocked["approvals"] == [] and not blocked["continuation"]["allowed"]
        failed = blocked["report"]["turns"][-1]
        assert failed["status"] == "failed" and failed["reason"] == "execution_unknown"
        assert failed["requests"] == [] and runtime.source.read_bytes() == expected
        assert blocked["report"]["undos"] == [undo]
    elif status == "interrupted":
        assert reopened["continuation"]["allowed"]
        # 重启本身没有重做；只有这次新的用户 /undo 才再次批准并修改文件。
        retried = runtime.run("undo", sid)
        assert [item["status"] for item in retried["report"]["undos"]] == [
            "interrupted",
            "applied",
        ]
        assert len(retried["approvals"]) == 1 and runtime.source.read_bytes() == runtime.original
        assert retried["report"]["undos"][1]["undo_id"] != undo["undo_id"]
    assert len(runtime.requests) == 3


@pytest.mark.parametrize("phase", ["recovery_write", "before_publish", "after_publish"])
def test_recovery_itself_can_be_killed_without_publishing_partial_log(undo_processes, phase):
    runtime = undo_processes
    edited = runtime.run("edit")
    sid = edited["session_id"]
    path = runtime.log_path(sid)
    with runtime.paused("undo", sid, "partial_finished") as process:
        runtime.kill(process)
    original_log = path.read_bytes()
    with runtime.paused("inspect", sid, phase) as process:
        runtime.kill(process)
    after_kill = path.read_bytes()
    if phase != "after_publish":
        assert after_kill == original_log
    else:
        assert after_kill.endswith(b"\n") and after_kill != original_log
    # 未发布的半份恢复文件只作遗留物；下次仍从原日志校验并构造完整恢复结果。
    leftovers = {p: p.read_bytes() for p in path.parent.glob("*.recovery.tmp")}
    assert bool(leftovers) is (phase != "after_publish")
    recovered = runtime.run("inspect", sid)
    assert_preserved(recovered, edited)
    assert recovered["report"]["undos"][0]["status"] == "unknown"
    assert recovered["approvals"] == [] and not recovered["continuation"]["allowed"]
    assert runtime.source.read_bytes() == runtime.original and len(runtime.requests) == 3
    backups = list(path.parent.glob("*.bak"))
    assert len(backups) == (1 if phase == "after_publish" else 2)
    assert all(p.read_bytes() == original_log for p in backups)
    assert {p: p.read_bytes() for p in path.parent.glob("*.recovery.tmp")} == leftovers
    assert len(recovered["report"]["recoveries"]) == 1
    stable = path.read_bytes()
    assert runtime.run("inspect", sid)["report"] == recovered["report"]
    assert path.read_bytes() == stable


@pytest.mark.parametrize("same_file", [True, False])
def test_repeated_undo_across_restarts_and_new_edit_selects_correct_candidate(
    undo_processes, same_file
):
    runtime = undo_processes
    other = runtime.workspace / "other.txt"
    other.write_bytes(b"other\r\n")
    first = runtime.run("edit")
    sid = first["session_id"]
    runtime.control.update(
        path="source.txt" if same_file else "other.txt",
        old="new" if same_file else "other",
        new="third",
    )
    second = runtime.run("edit", sid)
    edits = [turn["edits"][0] for turn in second["report"]["turns"]]
    last = runtime.run("undo", sid)
    assert last["report"]["undos"][-1]["edit_id"] == edits[1]["edit_id"]
    assert runtime.source.read_bytes() == runtime.original.replace(b"old", b"new")
    assert other.read_bytes() == b"other\r\n"
    earlier = runtime.run("undo", sid)
    assert earlier["report"]["undos"][-1]["edit_id"] == edits[0]["edit_id"]
    assert len(last["approvals"]) == len(earlier["approvals"]) == 1
    assert runtime.source.read_bytes() == runtime.original
    stable = runtime.log_path(sid).read_bytes()
    exhausted = runtime.run("undo", sid)
    assert "没有尚未撤销" in exhausted["error"]["message"] and exhausted["approvals"] == []
    assert runtime.log_path(sid).read_bytes() == stable
    runtime.control.update(path="source.txt", old="old", new="fresh")
    fresh = runtime.run("edit", sid)
    fresh_edit = fresh["report"]["turns"][-1]["edits"][0]
    final = runtime.run("undo", sid)
    assert final["report"]["undos"][-1]["edit_id"] == fresh_edit["edit_id"]
    assert runtime.source.read_bytes() == runtime.original
    assert [item["edit_id"] for item in final["report"]["undos"]] == [
        edits[1]["edit_id"],
        edits[0]["edit_id"],
        fresh_edit["edit_id"],
    ]
    assert all(item["status"] == "applied" for item in final["report"]["undos"])
    assert final["history"] == fresh["history"] and final["usage"] == fresh["usage"]
    assert len(runtime.requests) == 9 and final["report"]["recoveries"] == []


@pytest.mark.parametrize("phase", ["edit_started", "after_replace"])
def test_unknown_model_edit_blocks_undo_of_an_earlier_success(undo_processes, phase):
    """已有成功检查点也不能跨过后来结果未知的编辑，猜测应恢复到哪个版本。"""
    runtime = undo_processes
    first = runtime.run("edit")
    sid = first["session_id"]
    runtime.control.update(old="new", new="third")
    with runtime.paused("edit", sid, phase) as process:
        runtime.kill(process)
    expected = runtime.original.replace(b"old", b"new" if phase == "edit_started" else b"third")
    resumed = runtime.run("inspect", sid)
    assert resumed["error"] is None
    assert resumed["history"] == first["history"] and not resumed["usage"]["unknown"]
    assert resumed["report"]["turns"][-1]["edits"][0]["status"] == "unknown"
    checkpoints = runtime.workspace / "sessions" / "_checkpoints" / sid
    assert len(list(checkpoints.glob("*.json"))) == 2
    refused = runtime.run("undo", sid)
    assert refused["error"]["code"] == "execution_unknown"
    assert refused["approvals"] == [] and refused["report"]["undos"] == []
    assert not refused["continuation"]["allowed"]
    assert runtime.source.read_bytes() == expected and len(runtime.requests) == 5


def test_second_process_cannot_recover_or_undo_while_owner_holds_lock(undo_processes):
    runtime = undo_processes
    edited = runtime.run("edit")
    sid = edited["session_id"]
    with runtime.paused("undo", sid, "undo_prepared") as owner:
        competing = runtime.run("undo", sid)
        assert "已被其他进程占用" in competing["error"]["message"]
        assert competing["approvals"] == []
        assert "report" not in competing
        assert owner.poll() is None
        runtime.kill(owner)
    recovered = runtime.run("inspect", sid)
    assert_preserved(recovered, edited)
    assert [item["status"] for item in recovered["report"]["undos"]] == ["interrupted"]
    assert runtime.source.read_bytes() == runtime.original.replace(b"old", b"new")
    assert len(runtime.requests) == 3

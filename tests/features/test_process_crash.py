"""真实 HTTP 请求、强制终止与全新进程恢复；不访问外部模型或真实用户会话。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adam.storage import SessionStore


@pytest.fixture
def local_api():
    """本地真实 TCP 服务；可暂停响应以保证父进程在请求仍进行中时强杀。"""
    arrived, release = threading.Event(), threading.Event()
    calls = []
    control = {"hold_response": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            arrived.set()
            if control["hold_response"]:
                release.wait(20)
            message = {"role": "assistant", "content": "network answer"}
            if "edit_file" in control:
                name, arguments = "read_file", {"path": control["edit_file"]}
                if len(calls) == 2:
                    name = "replace_text"
                    arguments.update(
                        old_text="old",
                        new_text="new",
                        expected_version=json.loads(calls[-1]["messages"][-1]["content"])[
                            "version"
                        ],
                    )
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"edit-chain-{len(calls)}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            body = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                            "message": message,
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # 被强杀客户端关闭 TCP，属于本场景的预期结果。

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", arrived, calls, control
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def wait_for_boundary(process, ready, arrived, phase):
    """用服务端收到请求/子进程写入标志同步，不靠固定睡眠猜测到达位置。"""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if arrived.is_set() and (phase in {"request", "compact_request"} or ready.exists()):
            return
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=3)
            pytest.fail(f"worker exited before boundary: {stderr}")
        threading.Event().wait(0.03)
    pytest.fail("worker did not reach crash boundary")


@pytest.mark.parametrize("phase", ["request", "message_write", "after_commit"])
def test_real_request_kill_and_restart(tmp_path, local_api, phase):
    url, arrived, calls, control = local_api
    control["hold_response"] = phase == "request"
    root = tmp_path / "sessions"
    with SessionStore(root) as store:
        session_id = store.create_session("crash", "")
        store.append_turn(session_id, "old question", "old answer")
    path = next(root.glob("*/*/*/*.jsonl"))
    ready = tmp_path / "ready"
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, str(worker), "run", str(root), session_id, url, phase, str(ready)]
    process = subprocess.Popen(
        args,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        wait_for_boundary(process, ready, arrived, phase)
        process.kill()  # Windows TerminateProcess / POSIX SIGKILL，不执行 Python finally。
        process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    before_restart = path.read_bytes()
    assert before_restart.endswith(b"\n") == (phase != "message_write")
    args[2] = "resume"
    restarted = subprocess.run(
        args,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert restarted.returncode == 0, restarted.stderr
    result = json.loads(restarted.stdout)
    report = result["report"]
    crashed = report["turns"][1]
    request = crashed["requests"][0]
    assert len(calls) == 1  # 重启及预算拦截均没有再次访问服务端。
    assert calls[0]["messages"][-1]["content"] == "new question"
    assert result["history"][:2] == ["old question", "old answer"]
    assert report["unknown_usage"] == (phase == "request")
    assert report["known_tokens"] == (0 if phase == "request" else 7)
    backups = list(path.parent.glob("*.bak"))
    if phase == "after_commit":
        assert crashed["status"] == "succeeded" and crashed["reason"] == "completed"
        assert result["history"] == ["old question", "old answer", "new question", "network answer"]
        assert path.read_bytes() == before_restart
        assert backups == [] and report["recoveries"] == []
    else:
        assert crashed["status"] == "interrupted" and crashed["reason"] == "restart_recovery"
        assert crashed["duration_ms"] is None
        assert result["history"] == ["old question", "old answer"]
        assert [p.read_bytes() for p in backups] == [before_restart]
        assert len(report["recoveries"]) == 1
        assert report["recoveries"][0]["payload"]["tail_repaired"] == (phase == "message_write")
        if phase == "request":
            assert request["status"] == "interrupted" and request["duration_ms"] is None
            assert request["usage"] is None and result["blocked"]
        else:
            assert request["status"] == "succeeded" and request["reason"] == "completed"
            assert report["recoveries"][0]["payload"]["uncommitted_messages"] == 1


@pytest.mark.parametrize("phase", ["compact_request", "compact_write", "compact_commit"])
def test_summary_process_kill_and_restart(tmp_path, local_api, phase):
    """沿用真实本地 HTTP，在摘要请求、半行写入、完成标记后强杀并启动新进程。"""
    url, arrived, calls, control = local_api
    control["hold_response"] = phase == "compact_request"
    root = tmp_path / "sessions"
    with SessionStore(root) as store:
        session_id = store.create_session("summary crash", "")
        for index in range(5):
            store.append_turn(session_id, f"old {index}", "answer")
    path = next(root.rglob("*.jsonl"))
    original = path.read_bytes()
    ready = tmp_path / "ready"
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, str(worker), "run", str(root), session_id, url, phase, str(ready)]
    process = subprocess.Popen(
        args,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        wait_for_boundary(process, ready, arrived, phase)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    before_restart = path.read_bytes()
    assert before_restart.startswith(original)
    assert before_restart.endswith(b"\n") == (phase != "compact_write")
    args[2] = "resume"
    restarted = subprocess.run(
        args,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert restarted.returncode == 0, restarted.stderr
    result = json.loads(restarted.stdout)
    report = result["report"]
    operation = report["compactions"][0]
    assert len(calls) == 1 and "tools" not in calls[0]
    assert len(result["history"]) == 10
    assert report["unknown_usage"] == (phase == "compact_request")
    assert report["known_tokens"] == (0 if phase == "compact_request" else 7)
    assert operation["status"] == ("succeeded" if phase == "compact_commit" else "interrupted")
    assert operation["request"]["status"] == (
        "interrupted" if phase == "compact_request" else "succeeded"
    )
    with SessionStore(root) as store:
        summary = store.load_summary(session_id)
        assert (summary is not None) == (phase == "compact_commit")
    if phase == "compact_commit":
        assert report["recoveries"] == [] and path.read_bytes() == before_restart
    else:
        assert len(report["recoveries"]) == 1
        assert [item.read_bytes() for item in path.parent.glob("*.bak")] == [before_restart]
    if phase == "compact_request":
        assert result["blocked"]


@pytest.mark.parametrize(
    "phase,status,changed",
    [
        ("edit_plan", "not_started", False),
        ("edit_started", "unknown", False),
        ("edit_after_replace", "unknown", True),
        ("edit_committed", "applied", True),
    ],
)
def test_edit_process_kill_and_restart(tmp_path, local_api, phase, status, changed):
    """真实请求经读取与编辑后强杀；新进程恢复不能猜测替换是否完成。"""
    url, arrived, calls, control = local_api
    target = tmp_path / "sample.txt"
    target.write_bytes(b"old\n")
    control["edit_file"] = str(target)
    root, ready = tmp_path / "sessions", tmp_path / "ready"
    with SessionStore(root) as store:
        session_id = store.create_session("edit crash", "")
    path = next(root.rglob("*.jsonl"))
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, str(worker), "run", str(root), session_id, url, phase, str(ready)]
    options = dict(
        cwd=project,
        env=env,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    try:
        wait_for_boundary(process, ready, arrived, phase)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    before_restart = path.read_bytes()
    assert target.read_bytes() == (b"new\n" if changed else b"old\n")
    args[2] = "resume"
    restarted = subprocess.run(args, capture_output=True, timeout=15, **options)
    assert restarted.returncode == 0, restarted.stderr
    result = json.loads(restarted.stdout)
    report = result["report"]
    assert report["turns"][0]["edits"][0]["status"] == status
    assert report["turns"][0]["status"] == "interrupted"
    assert result["history"] == [] and not report["unknown_usage"]
    assert result["blocked"] is (status == "unknown")
    assert len(calls) == 2 and report["known_tokens"] == 14
    assert target.read_bytes() == (b"new\n" if changed else b"old\n")
    assert [item.read_bytes() for item in path.parent.glob("*.bak")] == [before_restart]

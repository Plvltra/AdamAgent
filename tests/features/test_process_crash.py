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
            body = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "network answer"},
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
        if arrived.is_set() and (phase == "request" or ready.exists()):
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

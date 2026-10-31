"""B01.3：新进程强杀并重开会话；本地 HTTP 服务核对真实请求次数，不使用付费模型。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.fixture
def progress_api():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            task = next(m["content"] for m in body["messages"] if m["role"] == "user")
            if task == "new task" and outputs(body):
                value = answer("still unreadable")
            else:
                value = tool_response([call(str(len(requests)), "read_file", path="invalid.txt")])
            encoded = json.dumps(value).encode("utf-8")
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
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize("phase,old_requests", [("warn", 2), ("stop", 3)])
def test_kill_after_saved_observation_then_resume_only_runs_explicit_new_turn(
    tmp_path, progress_api, phase, old_requests
):
    url, requests = progress_api
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "no_progress_worker.py"
    options = {
        "cwd": project,
        "text": True,
        "encoding": "utf-8",
        "env": {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), url, "kill", phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready.json").exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=3)
                pytest.fail(f"worker stopped before {phase}: {stdout}\n{stderr}")
            if time.monotonic() > deadline:
                pytest.fail("worker did not reach kill boundary")
            threading.Event().wait(0.02)
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert len(requests) == old_requests
    resumed = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), url, "resume", phase],
        capture_output=True,
        timeout=20,
        **options,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    report = json.loads(resumed.stdout)
    before = report["before"]
    assert before["usage"] == [old_requests * 12, False] and before["history"] == 0
    assert len(before["recovery"]) == 1
    recovery = before["recovery"][0]
    assert recovery["status"] == "interrupted" and recovery["reason"] == "restart_recovery"
    assert len(recovery["turn_ids"]) == 1 and recovery["request_ids"] == []
    assert recovery["tool_keys"] == []
    observed = [result["no_progress"] for result in report["results"]]
    assert [item["repeats"] for item in observed] == list(range(1, old_requests + 1)) + [1]
    assert len({item["fingerprint"] for item in observed}) == 1
    assert report["usage"] == [(old_requests + 2) * 12, False]
    assert len(requests) == old_requests + 2
    assert "old task" not in json.dumps(requests[-2:])

"""T9.3：独立进程真实请求后强杀，再独立进程重放；原数据保持只读，命令不会重跑。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adam.recording.journal import read_recording
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay_acceptance import command


@pytest.fixture
def recording_api():
    """本地服务观察真实请求；可阻塞首次响应，绝不访问远端模型。"""
    requests = []
    received, release = threading.Event(), threading.Event()
    blocking = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if blocking:
                received.set()
                release.wait(30)
                return
            value = (
                tool_response([call("command", "run_command", argv=command())])
                if len(requests) == 1
                else answer("command done")
            )
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
        yield f"http://127.0.0.1:{server.server_port}", requests, received, blocking
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "phase",
    ["request_inflight", "partial_tools", "tools_finished", "before_finish", "recording_finished"],
)
def test_killed_recording_replays_in_new_process_without_network_or_repeated_action(
    tmp_path, recording_api, phase
):
    url, requests, received, blocking = recording_api
    if phase == "request_inflight":
        blocking.append(True)
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "recording_worker.py"
    ready = tmp_path / "ready"
    options = {
        "cwd": project,
        "env": {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        "text": True,
        "encoding": "utf-8",
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), url, phase, str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    try:
        deadline = time.monotonic() + 20
        while not (received.is_set() if phase == "request_inflight" else ready.exists()):
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=3)
                pytest.fail(f"worker stopped before {phase}: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                pytest.fail(f"worker did not reach {phase}")
            threading.Event().wait(0.02)
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    # 强杀完成后才读取文件，避免观察者影响 Windows 上的原子文件操作。
    source = tmp_path / "capture.jsonl"
    source_bytes = source.read_bytes()
    session_file = next((tmp_path / "sessions").glob("*/*/*/*.jsonl"))
    session_bytes = session_file.read_bytes()
    recording = read_recording(source)
    complete = phase == "recording_finished"
    assert recording.complete is complete
    assert recording.torn_tail is (phase == "partial_tools")
    expected_calls = 2 if phase in {"before_finish", "recording_finished"} else 1
    assert len(requests) == expected_calls
    destination = tmp_path / "offline"
    replay = subprocess.run(
        [
            sys.executable,
            "-m",
            "adam.recordings",
            "replay",
            str(source),
            "--output",
            str(destination),
        ],
        capture_output=True,
        timeout=20,
        **options,
    )
    assert replay.returncode == (0 if complete else 2), replay.stderr + replay.stdout
    report = json.loads(replay.stdout)
    assert report["status"] == ("matched" if complete else "incomplete"), report
    assert json.loads((destination / "report.json").read_text(encoding="utf-8")) == report
    assert source.read_bytes() == source_bytes and session_file.read_bytes() == session_bytes
    assert len(requests) == expected_calls
    marker = tmp_path / "executions.txt"
    assert marker.exists() is (phase != "request_inflight")
    if marker.exists():
        assert marker.read_text() == "x"

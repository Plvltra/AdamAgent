"""T8.3：真实进程强杀验证正文发布与来源日志的先后关系，不重跑命令。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adam.session.storage import SessionStore
from tests.features.test_artifact_acceptance import command, prepare_log
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application, read_call
from tests.features.test_tool_loop import answer, tool_response


@pytest.fixture
def artifact_api():
    """使用本地 TCP 回应一次命令请求；重启读取不应再次访问此服务。"""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            result = tool_response([call("command", "run_command", argv=command())])
            encoded = json.dumps(result).encode("utf-8")
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
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("phase", ["published", "partial_record", "recorded", "tool_finished"])
def test_process_restart_preserves_only_published_and_recorded_references(
    tmp_path, artifact_api, phase
):
    prepare_log(tmp_path)
    url, requests = artifact_api
    root = tmp_path / "sessions"
    with SessionStore(root) as store:
        sid = store.create_session("attachment recovery", "")
        store.append_turn(sid, "old question", "old answer")
        original_history = [message.raw for message in store.load_messages(sid)]
    path = next(root.glob(f"*/*/*/{sid}.jsonl"))
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "artifact_worker.py"
    ready = tmp_path / "ready"
    args = [sys.executable, str(worker), "run", str(tmp_path), sid, url, phase, str(ready)]
    options = {
        "cwd": project,
        "env": {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        "text": True,
        "encoding": "utf-8",
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=3)
                pytest.fail(f"worker exited before {phase}: {stdout}\n{stderr}")
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

    # 只在写进程退出后读日志，避免 Windows 打开句柄影响生产原子替换。
    original_log = path.read_bytes()
    assert original_log.endswith(b"\n") == (phase != "partial_record")
    artifact = next((root / "_artifacts").glob("*/*.json"))
    original_artifact = artifact.read_bytes()
    bundle = json.loads(original_artifact)
    reference = bundle["reference"]
    args[2] = "resume"
    restarted = subprocess.run(args, capture_output=True, timeout=20, **options)
    assert restarted.returncode == 0, restarted.stderr
    result = json.loads(restarted.stdout)
    assert result["history"] == original_history
    assert result["usage"] == [12, False]
    assert len(requests) == 1 and (tmp_path / "executions.txt").read_text() == "x"
    last = result["report"]["turns"][-1]
    assert last["status"] == "interrupted" and last["reason"] == "restart_recovery"
    assert last["requests"][0]["status"] == "succeeded"
    assert last["commands"][0]["exit_code"] == 0
    assert last["tools"][0]["status"] == ("finished" if phase == "tool_finished" else "interrupted")
    readable = phase in {"recorded", "tool_finished"}
    assert len(result["references"]) == int(readable)
    assert [item.read_bytes() for item in path.parent.glob("*.bak")] == [original_log]
    assert artifact.read_bytes() == original_artifact

    def respond(body, number):
        if number == 1:
            return tool_response(
                [read_call(reference, offset=reference["saved_chars"] - 240, count=240)]
            )
        output = outputs(body)[-1]
        assert output["ok"] is readable
        if readable:
            assert output["complete"] and "ANSWER=42" in output["text"]
        else:
            assert "执行来源" in output["error"]
        return answer("checked recovered reference")

    with application(tmp_path, respond) as (app, store, calls):
        app.resume(sid)
        assert app.send("explicitly inspect the saved reference") == "checked recovered reference"
        assert len(calls) == 2 and len(store.load_executions(sid)) == 1
    assert (tmp_path / "executions.txt").read_text() == "x"
    assert artifact.read_bytes() == original_artifact

"""V5.2：独立 CLI 进程在真实 HTTP/子进程等待中收到 SIGINT，回到输入并可新建会话。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def frame(delta=None, *, reason=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}],
                "usage": usage,
            }
        )
        + "\n\n"
    ).encode()


@pytest.fixture
def cancel_server(tmp_path):
    ready, release = threading.Event(), threading.Event()
    requests, control = [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            mode = control["mode"]
            first = len(requests) == 1
            if first and mode == "headers":
                ready.set()
                release.wait(20)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if first and mode in {"body", "known_body"}:
                    self.wfile.write(frame({"content": "partial"}))
                    if mode == "known_body":
                        self.wfile.write(
                            frame(reason="stop", usage={"prompt_tokens": 4, "completion_tokens": 2})
                        )
                    self.wfile.flush()
                    ready.set()
                    release.wait(20)
                elif first and mode in {"command", "confirm"}:
                    argv = [sys.executable, str(tmp_path / "tree.py")]
                    call = {
                        "index": 0,
                        "id": "command",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps({"argv": argv}),
                        },
                    }
                    self.wfile.write(frame({"tool_calls": [call]}))
                    self.wfile.write(
                        frame(
                            reason="tool_calls", usage={"prompt_tokens": 4, "completion_tokens": 2}
                        )
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.wfile.write(frame({"content": "done"}))
                    self.wfile.write(
                        frame(reason="stop", usage={"prompt_tokens": 4, "completion_tokens": 2})
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", ready, release, requests, control
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def alive(pid):
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return f'"{pid}"'.encode() in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("mode", ["headers", "body", "known_body", "command", "confirm"])
def test_interrupt_real_io_keeps_cli_and_records_state(tmp_path, cancel_server, mode):
    url, ready, release, requests, control = cancel_server
    control["mode"] = mode
    (tmp_path / "config.toml").write_text('[storage]\nsessions_dir="sessions"\n', encoding="utf-8")
    (tmp_path / "llm.toml").write_text(
        f'[llm]\nbase_url="{url}"\nmodel="test"\napi_key="key"\ntimeout_seconds=15\n',
        encoding="utf-8",
    )
    (tmp_path / "tree.py").write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path('pids').write_text(str(os.getpid()) + ',' + str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "cancel_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), mode],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    pids = []
    try:
        if mode != "confirm":
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (
                    (mode == "command" and (tmp_path / "pids").exists())
                    or (mode not in {"command", "known_body"} and ready.is_set())
                    or (mode == "known_body" and (tmp_path / "usage_received").exists())
                ):
                    break
                assert process.poll() is None
                time.sleep(0.02)
            else:
                raise AssertionError("worker did not reach interrupt boundary")
            if mode == "command":
                pids = [int(item) for item in (tmp_path / "pids").read_text().split(",")]
            # 等待正文进入客户端；测试投递与物理键盘相同的 Python SIGINT 处理路径。
            time.sleep(0.2)
            (tmp_path / "cancel").touch()
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        interrupted = next(
            report for report in result["reports"] if report["turns"][0]["status"] == "interrupted"
        )
        turn = interrupted["turns"][0]
        assert turn["reason"] == "interrupted"
        assert "已取消本轮" in "\n".join(result["output"])
        assert len(result["reports"]) == 2
        assert all(not alive(pid) for pid in pids)
        assert interrupted["unknown_usage"] is (mode in {"headers", "body"})
        assert "继续方式>" in "\n".join(result["output"])
        assert len(requests) == (2 if mode in {"headers", "body"} else 3)
        if mode == "command":
            assert turn["commands"][0]["status"] == "interrupted"
        elif mode == "confirm":
            assert turn["commands"] == []
        else:
            assert turn["requests"][0]["status"] == "interrupted"
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        if os.name == "nt":
            for pid in pids:
                if alive(pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )

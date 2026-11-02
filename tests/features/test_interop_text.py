"""C09.1 通过两个真实进程和真实回环 HTTP 验收，不使用 MockTransport。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from adam.interop.client import TextAgentClient
from adam.interop.models import InteropError

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "tests/features/support/interop_worker.py"


@pytest.fixture
def endpoint(tmp_path):
    ready = tmp_path / "port"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    process = subprocess.Popen(
        [sys.executable, str(WORKER), "server", str(ready)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read())
            if time.monotonic() > deadline:
                pytest.fail("server readiness timeout")
            time.sleep(0.02)
        info = json.loads(ready.read_text(encoding="utf-8"))
        yield f"http://127.0.0.1:{info['port']}", info["pid"], env
    finally:
        process.communicate("stop\n", timeout=5)


def test_two_real_processes_discover_and_complete_text(endpoint):
    """客户端与服务端独立启动，核对卡片、实际计算结果和不同 PID。"""
    origin, server_pid, env = endpoint
    result = subprocess.run(
        [sys.executable, str(WORKER), "client", origin],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence["client_pid"] not in (server_pid, os.getpid())
    assert evidence["card"]["protocol_version"] == "0.3.0"
    assert evidence["card"]["skills"] == ["text"]
    assert evidence["result"]["message_id"]
    assert json.loads(evidence["result"]["text"]) == {"words": 3, "server_pid": server_pid}


def test_remote_business_failure_and_unsupported_method(endpoint):
    origin, _, _ = endpoint
    with pytest.raises(InteropError, match="协议错误"):
        TextAgentClient(origin).send("fail")
    with httpx.Client(trust_env=False) as client:
        reply = client.post(
            origin, json={"jsonrpc": "2.0", "id": "x", "method": "message/stream"}
        ).json()
    assert reply["id"] == "x"
    assert reply["error"]["code"] == -32601
    assert "private failure" not in str(reply)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "http://localhost:80",
        "http://127.0.0.1",
        "http://user@127.0.0.1:80",
        "http://127.0.0.1:80/path",
    ],
)
def test_reject_unapproved_origins_before_network(origin):
    with pytest.raises(InteropError, match="回环"):
        TextAgentClient(origin)


def test_non_text_input_rejected_without_running_handler(endpoint):
    origin, _, _ = endpoint
    with httpx.Client(trust_env=False) as client:
        reply = client.post(
            origin,
            json={
                "jsonrpc": "2.0",
                "id": "bad",
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": "id",
                        "parts": [{"kind": "file", "file": {"uri": "secret"}}],
                    }
                },
            },
        ).json()
    assert reply["error"]["code"] == -32602
    assert "result" not in reply

"""E7.3：真实服务失败、断开、超时与取消；检查调用次数和进程退出。"""

import json
import threading
import time
from contextlib import ExitStack
from dataclasses import replace

import pytest

from adam.config.mcp import MCPConfig, MCPServerConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.mcp.client import MCPConnection
from adam.mcp.schema import validator_for
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.mcp_tools import register_mcp
from tests.features.test_mcp_connection import SERVICE, assert_stopped, connection
from tests.features.test_mcp_tools import DEMO


@pytest.mark.parametrize("mode", ["error", "slow", "disconnect"])
def test_real_fault_is_not_retried_and_process_is_reaped(tmp_path, mode):
    client = connection(tmp_path).open()
    pid = int((tmp_path / "server.pid").read_text(encoding="utf-8"))
    try:
        if mode == "error":
            result = client.call("probe", {"mode": mode})
            assert not result["ok"] and result["error"]
        else:
            client.config = replace(client.config, timeout_seconds=0.2)
            with pytest.raises(ToolError):
                client.call("probe", {"mode": mode})
    finally:
        client.close()
    assert_stopped(pid)
    assert (tmp_path / "server.calls").read_text(encoding="utf-8").splitlines() == [mode]


def test_cancel_actual_inflight_query_closes_server(tmp_path):
    client = connection(tmp_path).open()
    pid = int((tmp_path / "server.pid").read_text(encoding="utf-8"))
    cancel = CancellationToken()

    def cancel_when_started():
        deadline = time.monotonic() + 5
        while not (tmp_path / "server.calls").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.cancel()

    stopper = threading.Thread(target=cancel_when_started, daemon=True)
    stopper.start()
    try:
        with pytest.raises(RequestInterrupted):
            client.call("probe", {"mode": "slow"}, cancel)
    finally:
        stopper.join(timeout=6)
        client.close()
    assert_stopped(pid)
    assert (tmp_path / "server.calls").read_text(encoding="utf-8").splitlines() == ["slow"]


def test_non_readonly_service_is_rejected_and_cleaned(tmp_path):
    import sys

    config = MCPConfig(
        True,
        (
            MCPServerConfig(
                "unsafe",
                sys.executable,
                (str(SERVICE), "--pid-file", str(tmp_path / "server.pid")),
                ("unsafe",),
            ),
        ),
    )
    with pytest.raises(ToolError, match="只读"), ExitStack() as resources:
        register_mcp(create_tool_registry(tmp_path), config, tmp_path, resources)
    assert_stopped(int((tmp_path / "server.pid").read_text(encoding="utf-8")))


def test_remote_schema_reference_is_rejected_without_network():
    with pytest.raises(ToolError, match="内部引用"):
        validator_for({"type": "object", "$ref": "https://unused.invalid/schema.json"})


def test_demo_service_checks_escape_and_credentials(tmp_path):
    import sys

    (tmp_path / ".env.local").write_text("do-not-return", encoding="utf-8")
    client = MCPConnection(
        MCPServerConfig(
            "files",
            sys.executable,
            (str(DEMO), "--workspace", str(tmp_path)),
            ("read_project_text",),
        ),
        tmp_path,
    ).open()
    try:
        for path in ("../outside.txt", ".env.local"):
            result = client.call("read_project_text", {"path": path})
            assert not result["ok"]
            assert "do-not-return" not in json.dumps(result)
    finally:
        client.close()


def test_direct_and_protocol_measurement_uses_same_result(tmp_path):
    from scripts.compare_mcp import compare

    (tmp_path / "note.txt").write_text("short query", encoding="utf-8")
    report = compare(tmp_path, "note.txt", runs=2)
    assert report["same_result"] and report["runs"] == 2
    assert report["cold_start_seconds"] > 0
    assert report["direct_mean_seconds"] >= 0 and report["mcp_mean_seconds"] > 0
    assert report["mcp_result_chars"] > report["direct_result_chars"]

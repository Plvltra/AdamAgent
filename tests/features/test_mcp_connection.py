"""E7.1：真实官方 SDK 子进程，握手、复用、关闭及启动故障。"""

import ctypes
import json
import os
import sys
import time
from pathlib import Path

import pytest

from adam.config.mcp import MCPServerConfig, parse_mcp
from adam.contracts.errors import ConfigError, ToolError
from adam.mcp.client import MCPConnection

SERVICE = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_service.py"


def connection(root, **changes):
    config = MCPServerConfig(
        alias="sample",
        command=sys.executable,
        args=(str(SERVICE), "--pid-file", str(root / "server.pid")),
        tools=("probe",),
        **changes,
    )
    return MCPConnection(config, root)


def assert_stopped(pid):
    """Windows 等待实际进程句柄；不把取消 Future 当成服务已退出。"""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, 0, pid)
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 5000) == 0
            finally:
                kernel.CloseHandle(handle)
    else:
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail("server process did not exit")


def test_real_stdio_connection_reuses_server_and_closes_it(tmp_path):
    client = connection(tmp_path).open()
    try:
        definitions = client.definitions()
        assert definitions[0]["name"] == "probe"
        first = client.call("probe", {"data": {"value": 3}})
        second = client.call("probe", {})
        assert first["ok"] and first["structured_content"]["data"] == {"value": 3}
        pid = first["structured_content"]["pid"]
        assert second["structured_content"]["pid"] == pid
    finally:
        client.close()
    client.close()
    assert_stopped(pid)
    with pytest.raises(ToolError, match="关闭"):
        client.call("probe", {})


def test_toml_loading_does_not_start_a_service(tmp_path):
    from adam.config.loader import load_config

    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="dummy"\n',
        encoding="utf-8",
    )
    path = tmp_path / "config.toml"
    path.write_text(
        '[mcp]\nenabled=true\n[[mcp.servers]]\nalias="sample"\ncommand='
        + json.dumps(sys.executable)
        + '\ntools=["probe"]\n',
        encoding="utf-8",
    )
    parsed = load_config(path).mcp
    assert parsed.enabled and parsed.servers[0].command == sys.executable
    assert not (tmp_path / "server.pid").exists()


def test_start_failure_is_bounded_and_config_rejects_ambiguous_aliases(tmp_path):
    client = MCPConnection(
        MCPServerConfig("missing", "AdamMissingProgram", tools=("read",)), tmp_path
    )
    started = time.monotonic()
    with pytest.raises(ToolError, match="启动"):
        client.open()
    client.close()
    assert time.monotonic() - started < 10
    entry = {"alias": "same", "command": sys.executable, "tools": ["probe"]}
    with pytest.raises(ConfigError, match="重复"):
        parse_mcp({"enabled": True, "servers": [entry, entry]})
    assert not parse_mcp({}).enabled

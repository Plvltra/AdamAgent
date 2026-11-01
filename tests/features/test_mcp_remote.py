"""B15.3.1：真实回环 HTTP、认证、固定端点、取消及与原会话/重放的组合。"""

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from adam.config.mcp import MCPConfig, MCPServerConfig, parse_mcp
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted, ToolError
from adam.mcp.client import MCPConnection
from adam.recording.runtime import replay_turn
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

SERVICE = Path(__file__).resolve().parents[1] / "fixtures/mcp_http_service.py"
TOKEN = "test-mcp-bearer-123"


@contextmanager
def remote_server(root, scenario="normal"):
    """测试拥有服务进程；客户端只关闭连接，最后由夹具终止测试服务。"""
    with (root / "server.err").open("wb") as errors:
        process = subprocess.Popen(
            [sys.executable, str(SERVICE), "--root", str(root), "--scenario", scenario],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 10
            url = None
            # 仅测试健康探针接受自签名证书；被验收的 MCP 生产连接仍须拒绝它。
            with httpx.Client(trust_env=False, timeout=0.2, verify=scenario != "tls") as http:
                while time.monotonic() < deadline:
                    assert process.poll() is None, (root / "server.err").read_text(errors="replace")
                    path = root / "url.txt"
                    if path.exists():
                        url = path.read_text(encoding="utf-8")
                        try:
                            if http.get(url.removesuffix("/mcp") + "/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                    time.sleep(0.02)
                else:
                    pytest.fail("MCP HTTP service did not start")
            assert url
            yield url, process
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)


def calls(root):
    path = root / "http.calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("scenario", ["normal", "sse"])
def test_real_http_supports_tools_resources_and_prompts_on_one_connection(
    tmp_path, scenario, monkeypatch
):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with remote_server(tmp_path, scenario) as (url, process):
        config = MCPServerConfig(
            "remote", tools=("ping",), resources=("docs://hello",), prompts=("review",), url=url
        )
        client = MCPConnection(config, tmp_path).open()
        try:
            assert client.list_resources()["resources"][0]["description"] == ""
            assert client.call("ping", {"text": "hello"})["content"] == ["pong:hello"]
            assert client.read_resource("docs://hello")["contents"][0]["text"] == "HTTP resource"
            assert (
                client.get_prompt("review", {"text": "code"})["messages"][0]["text"]
                == "Review: code"
            )
        finally:
            client.close()
        assert process.poll() is None
        sent = calls(tmp_path)
        assert all(item["path"] == "/mcp" for item in sent)
        assert len([item for item in sent if item["rpc"] == "tools/call"]) == 1


def test_authentication_stays_out_of_config_logs_model_input_and_recording(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    with remote_server(tmp_path, "auth") as (url, process):
        server = MCPServerConfig(
            "remote", tools=("ping",), url=url, token_env="ADAM_TEST_MCP_TOKEN"
        )
        config = replace(config_at(tmp_path), mcp=MCPConfig(True, (server,)))
        assert TOKEN not in json.dumps(asdict(server)) and TOKEN not in repr(server)

        def respond(body, number):
            assert TOKEN not in json.dumps(body)
            if number == 1:
                result = tool_response([call("ping", "mcp_remote_ping", text="hello")])
            else:
                assert outputs(body)[-1]["content"] == ["pong:hello"]
                result = answer("remote checked")
            return httpx.Response(200, json=result)

        with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, _):
            assert recorder.run(lambda: app.send("task")) == "remote checked"
            assert TOKEN not in json.dumps(store.load_events(app.session.session_id))
        assert TOKEN not in path.read_text(encoding="utf-8")
        assert process.poll() is None and all(item["authorized"] for item in calls(tmp_path))
    assert TOKEN not in caplog.text
    assert TOKEN not in (tmp_path / "server.err").read_text(errors="replace")
    forbid_external_actions(monkeypatch)
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize("value", [None, "", "secret\nheader", "秘密"])
def test_missing_or_invalid_auth_fails_before_http(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ADAM_TEST_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", value)
    with remote_server(tmp_path) as (url, _):
        client = MCPConnection(
            MCPServerConfig("remote", tools=("ping",), url=url, token_env="ADAM_TEST_MCP_TOKEN"),
            tmp_path,
        )
        with pytest.raises(ToolError, match="认证环境变量") as error:
            client.open()
        assert not calls(tmp_path)
        if value:
            assert value not in str(error.value)
        client.close()


@pytest.mark.parametrize("scenario", ["auth", "redirect", "cross_redirect"])
def test_rejected_handshake_never_follows_another_endpoint(tmp_path, scenario):
    with remote_server(tmp_path, scenario) as (url, _):
        client = MCPConnection(
            MCPServerConfig("remote", tools=("ping",), url=url, timeout_seconds=1), tmp_path
        )
        with pytest.raises(ToolError):
            client.open()
        client.close()
        sent = calls(tmp_path)
        assert sent and all(item["path"] == "/mcp" for item in sent)
        assert not any(item["rpc"] == "tools/call" for item in sent)


@pytest.mark.parametrize("scenario", ["slow", "error", "malformed"])
def test_failed_remote_action_does_not_repeat_business_post(tmp_path, scenario):
    with remote_server(tmp_path, scenario) as (url, process):
        client = MCPConnection(
            MCPServerConfig("remote", tools=("ping",), url=url, timeout_seconds=1), tmp_path
        ).open()
        try:
            with pytest.raises(ToolError):
                client.call("ping", {"text": "hello"})
            with pytest.raises(ToolError, match="关闭"):
                client.call("ping", {"text": "again"})
        finally:
            client.close()
        assert process.poll() is None
        assert len([item for item in calls(tmp_path) if item["rpc"] == "tools/call"]) == 1


def test_cancel_remote_request_closes_connection_without_claiming_server_stopped(tmp_path):
    with remote_server(tmp_path, "slow") as (url, process):
        client = MCPConnection(MCPServerConfig("remote", tools=("ping",), url=url), tmp_path).open()
        token = CancellationToken()

        def cancel_on_receipt():
            deadline = time.monotonic() + 5
            while (
                not any(c["rpc"] == "tools/call" for c in calls(tmp_path))
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            token.cancel()

        thread = threading.Thread(target=cancel_on_receipt)
        thread.start()
        try:
            with pytest.raises(RequestInterrupted):
                client.call("ping", {"text": "hello"}, cancel=token)
        finally:
            thread.join(timeout=6)
            client.close()
        assert not thread.is_alive() and process.poll() is None
        assert len([c for c in calls(tmp_path) if c["rpc"] == "tools/call"]) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "http://example.com/mcp"},
        {"url": "http://localhost/mcp"},
        {"url": "file:///C:/secret"},
        {"url": "https://user:password@example.com/mcp"},
        {"url": "https://example.com/mcp?token=secret"},
        {"url": "https://example.com/mcp#secret"},
        {"url": "https://example.com:bad/mcp"},
        {"url": "https://example.com:0/mcp"},
        {"url": "https://example.com/mcp\n"},
        {"url": "https://example.com", "command": "python"},
        {"url": "https://example.com", "args": ["script.py"]},
        {"url": "https://example.com", "token_env": "not a name"},
        {"command": "python", "token_env": "TOKEN"},
    ],
)
def test_remote_configuration_rejects_implicit_or_ambiguous_authority(extra):
    with pytest.raises(ConfigError):
        parse_mcp({"servers": [{"alias": "remote", "tools": ["ping"], **extra}]})


def test_self_signed_tls_is_rejected_before_protocol_or_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    with remote_server(tmp_path, "tls") as (url, process):
        client = MCPConnection(
            MCPServerConfig(
                "remote",
                tools=("ping",),
                url=url,
                token_env="ADAM_TEST_MCP_TOKEN",
                timeout_seconds=0.5,
            ),
            tmp_path,
        )
        try:
            with pytest.raises(ToolError):
                client.open()
        finally:
            client.close()
        assert process.poll() is None and not calls(tmp_path)


def test_https_configuration_does_not_connect_while_disabled(tmp_path, monkeypatch):
    config = parse_mcp(
        {"servers": [{"alias": "remote", "url": "https://example.com/mcp", "tools": ["ping"]}]}
    )
    assert not config.enabled and config.servers[0].command == ""
    monkeypatch.setattr(MCPConnection, "open", lambda *_: pytest.fail("disabled remote connected"))
    with captured_app(
        tmp_path,
        lambda *_: httpx.Response(200, json=answer("ok")),
        config=replace(config_at(tmp_path), mcp=config),
    ) as (app, recorder, _, _, _):
        assert recorder.run(lambda: app.send("task")) == "ok"

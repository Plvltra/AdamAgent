"""B15.3.3：有认证的真实 HTTP JSON/SSE 混合媒体、持久化、故障与原工具回归。"""

import base64
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest

from adam.config.mcp import MCPConfig, MCPServerConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.mcp.client import MCPConnection
from adam.mcp.media_store import MCPMediaStore
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_mcp_media_results import references
from tests.features.test_mcp_remote import TOKEN, calls, remote_server
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

DATA = b"HTTP media fixture\x00\xff\x80"


def server_config(url):
    return MCPServerConfig(
        "remote",
        tools=("ping", "media"),
        resources=("docs://binary",),
        prompts=("visual",),
        url=url,
        token_env="ADAM_TEST_MCP_TOKEN",
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_remote_tool_resource_prompt_preserve_bytes_without_fetching_links(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    store = MCPMediaStore(tmp_path / "media")
    with remote_server(tmp_path, "media_" + transport) as (url, process):
        client = MCPConnection(server_config(url), tmp_path, media_store=store).open()
        try:
            assert client.call("ping", {"text": "original"})["content"] == ["pong:original"]
            tool = client.call("media", {})
            resource = client.read_resource("docs://binary")
            prompt = client.get_prompt("visual", {})
            assert client.read_resource("docs://binary", resource["version"]) == resource
            assert tool["references"][-1]["read_allowed"] is False
            assert tool["references"][-1]["content_loaded"] is False
            refs = list(references([tool, resource, prompt]))
            assert len(refs) == 5 and len({r["media_id"] for r in refs}) == 5
            assert len({r["source"]["server_id"] for r in refs}) == 1
            assert {r["source"]["kind"] for r in refs} == {"tool", "resource", "prompt"}
            assert all(store.read(ref) == DATA for ref in refs)
            encoded = json.dumps([tool, resource, prompt])
            assert TOKEN not in encoded and base64.b64encode(DATA).decode() not in encoded
        finally:
            client.close()
        sent = calls(tmp_path)
        assert all(item["authorized"] and item["path"] == "/mcp" for item in sent)
        assert [v["rpc"] for v in sent].count("resources/read") == 2
        assert [v["rpc"] for v in sent].count("tools/call") == 2
        assert process.poll() is None
    assert all(MCPMediaStore(store.root).read(ref) == DATA for ref in refs)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("fault", ["bad", "quota"])
def test_remote_bad_body_or_full_store_keeps_existing_reference_without_retry(
    tmp_path, monkeypatch, transport, fault
):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    store = MCPMediaStore(tmp_path / "media", max_items=1 if fault == "quota" else 128)
    scenario = "media_" + ("bad_" if fault == "bad" else "") + transport
    with remote_server(tmp_path, scenario) as (url, process):
        client = MCPConnection(server_config(url), tmp_path, media_store=store).open()
        try:
            previous = next(references(client.read_resource("docs://binary")))
            existing = list(store.root.glob("*.bin"))
            with pytest.raises(ToolError):
                client.call("media", {})
            assert list(store.root.glob("*.bin")) == existing
            assert store.read(previous) == DATA
        finally:
            client.close()
        assert process.poll() is None
        assert [v["rpc"] for v in calls(tmp_path)].count("tools/call") == 1


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_remote_media_chat_records_refs_and_replays_without_network(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    with remote_server(tmp_path, "media_" + transport) as (url, process):
        config = replace(config_at(tmp_path), mcp=MCPConfig(True, (server_config(url),)))
        saved = []

        def respond(body, number):
            encoded = json.dumps(body)
            assert TOKEN not in encoded and base64.b64encode(DATA).decode() not in encoded
            if number == 1:
                response = tool_response(
                    [
                        call("ping", "mcp_remote_ping", text="old tool"),
                        call("tool", "mcp_remote_media"),
                        call("resource", "mcp_remote_read_resource", uri="docs://binary"),
                        call("prompt", "mcp_remote_get_prompt", name="visual", arguments={}),
                    ]
                )
            else:
                values = outputs(body)
                assert len(values) == 4 and all(v["ok"] for v in values)
                assert values[0]["content"] == ["pong:old tool"]
                saved.extend(references(values))
                assert len(saved) == 5
                response = answer("媒体已保存，内容尚未解释")
            return httpx.Response(200, json=response)

        with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, _):
            assert recorder.run(lambda: app.send("task")) == "媒体已保存，内容尚未解释"
            sid = app.session.session_id
            events = store.load_events(sid)
            assert len([e for e in events if e["type"] == "tool_finished"]) == 4
            assert TOKEN not in json.dumps(events)
        assert process.poll() is None
    with SessionStore(config.sessions_dir) as store:
        store.select_session(sid)
        assert store.load_events(sid) == events
    media = MCPMediaStore(config.sessions_dir / "_mcp_media")
    assert all(media.read(ref) == DATA for ref in saved)
    assert TOKEN not in path.read_text(encoding="utf-8")
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        "httpx2.AsyncClient.send", lambda *a, **k: pytest.fail("replay attempted MCP HTTP")
    )
    assert replay_turn(path, tmp_path / "offline")["status"] == "matched"
    assert [v["rpc"] for v in calls(tmp_path)].count("tools/call") == 2


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_cancel_remote_media_keeps_previous_bytes_and_closes_only_connection(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setenv("ADAM_TEST_MCP_TOKEN", TOKEN)
    media = MCPMediaStore(tmp_path / "media")
    with remote_server(tmp_path, "media_slow_" + transport) as (url, process):
        client = MCPConnection(server_config(url), tmp_path, media_store=media).open()
        previous = next(references(client.read_resource("docs://binary")))
        token = CancellationToken()

        def cancel_after_received():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(item["rpc"] == "tools/call" for item in calls(tmp_path)):
                    token.cancel()
                    return
                time.sleep(0.02)

        stopper = threading.Thread(target=cancel_after_received)
        stopper.start()
        try:
            with pytest.raises(RequestInterrupted):
                client.call("media", {}, cancel=token)
        finally:
            stopper.join(timeout=6)
            client.close()
        assert not stopper.is_alive() and process.poll() is None
        assert len(list(media.root.glob("*.bin"))) == 1 and media.read(previous) == DATA
        assert [v["rpc"] for v in calls(tmp_path)].count("tools/call") == 1

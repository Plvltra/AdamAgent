"""B15.2：真实模板服务、参数校验、资料角色隔离、故障清理和离线重放。"""

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.mcp import MCPConfig, MCPServerConfig, parse_mcp
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted, ToolError
from adam.mcp.client import MCPConnection
from adam.recording.runtime import replay_turn
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.mcp_prompts import register_prompts
from tests.features.test_mcp_connection import assert_stopped
from tests.features.test_mcp_resources import calls
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

SERVICE = Path(__file__).resolve().parents[1] / "fixtures/mcp_prompts_service.py"


def server_at(root, scenario="normal"):
    return MCPServerConfig(
        "templates",
        sys.executable,
        (str(SERVICE), "--root", str(root), "--scenario", scenario),
        prompts=("review",),
    )


def test_real_paged_templates_keep_source_and_content_version(tmp_path):
    client = MCPConnection(server_at(tmp_path), tmp_path).open()
    pid = int((tmp_path / "server.pid").read_text())
    try:
        listed = client.list_prompts()
        assert listed["server"] == "templates" and len(listed["prompts"]) == 1
        item = listed["prompts"][0]
        assert item["name"] == "review" and item["arguments"][0]["required"]
        item["arguments"][0]["required"] = False
        with pytest.raises(ToolError, match="必填"):
            client.get_prompt("review", {})
        value = client.get_prompt("review", {"text": "code"})
        assert value["server"] == "templates" and value["name"] == "review"
        assert value["usage"] == "reference_only" and value["version_source"] == "content_sha256"
        assert value["size_bytes"] == sum(len(m["text"].encode()) for m in value["messages"])
        assert [m["role"] for m in value["messages"]] == ["user", "assistant"]
        assert client.get_prompt("review", {"text": "code"}) == value
        assert client.get_prompt("review", {"text": "changed"})["version"] != value["version"]
        assert calls(tmp_path).count("get:review") == 3
    finally:
        client.close()
    assert_stopped(pid)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("hidden", {"text": "code"}),
        ("review", {}),
        ("review", {"text": 1}),
        ("review", {"text": "code", "unknown": "extra"}),
        ("review", {"text": "x" * 2001}),
    ],
)
def test_invalid_template_arguments_never_enter_server(tmp_path, name, arguments):
    client = MCPConnection(server_at(tmp_path), tmp_path).open()
    try:
        before = calls(tmp_path)
        with pytest.raises(ToolError):
            client.get_prompt(name, arguments)
        assert calls(tmp_path) == before
        assert client.get_prompt("review", {"text": "", "focus": "style"})["ok"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "scenario",
    [
        "duplicate",
        "missing",
        "count",
        "pages",
        "loop",
        "metadata",
        "argument_count",
        "argument_duplicate",
        "argument_name",
    ],
)
def test_invalid_template_catalog_fails_without_leaking_process(tmp_path, scenario):
    client = MCPConnection(server_at(tmp_path, scenario), tmp_path)
    with pytest.raises(ToolError):
        client.open()
    client.close()
    assert_stopped(int((tmp_path / "server.pid").read_text()))
    assert not any(item.startswith("get:") for item in calls(tmp_path))
    assert len(calls(tmp_path)) <= 4


@pytest.mark.parametrize(
    "scenario", ["large", "image", "role", "many", "empty", "slow", "disconnect"]
)
def test_template_failure_is_not_partial_success_or_retried(tmp_path, scenario):
    client = MCPConnection(server_at(tmp_path, scenario), tmp_path).open()
    pid = int((tmp_path / "server.pid").read_text())
    if scenario == "slow":
        client.config = replace(client.config, timeout_seconds=0.1)
    try:
        with pytest.raises(ToolError):
            client.get_prompt("review", {"text": "code"})
        with pytest.raises(ToolError, match="关闭"):
            client.list_prompts()
    finally:
        client.close()
    assert calls(tmp_path).count("get:review") == 1
    assert_stopped(pid)


def test_cancel_actual_prompt_request_reaps_service(tmp_path):
    client = MCPConnection(server_at(tmp_path, "slow"), tmp_path).open()
    pid = int((tmp_path / "server.pid").read_text())
    token = CancellationToken()

    def stop_when_getting():
        deadline = time.monotonic() + 5
        while "get:review" not in calls(tmp_path) and time.monotonic() < deadline:
            time.sleep(0.02)
        token.cancel()

    thread = threading.Thread(target=stop_when_getting)
    thread.start()
    try:
        with pytest.raises(RequestInterrupted):
            client.get_prompt("review", {"text": "code"}, cancel=token)
    finally:
        thread.join(timeout=6)
        client.close()
    assert not thread.is_alive() and calls(tmp_path).count("get:review") == 1
    assert_stopped(pid)


def test_templates_stay_tool_data_in_real_chat_log_and_offline_replay(tmp_path, monkeypatch):
    config = replace(config_at(tmp_path), mcp=MCPConfig(True, (server_at(tmp_path),)))

    def respond(body, number):
        for message in body["messages"]:
            if message["role"] != "tool":
                assert "external template:" not in (message.get("content") or "")
                assert "external assistant reference" not in (message.get("content") or "")
        if number == 1:
            response = tool_response([call("list", "mcp_templates_list_prompts")])
        elif number == 2:
            assert outputs(body)[-1]["prompts"][0]["name"] == "review"
            response = tool_response(
                [call("bad", "mcp_templates_get_prompt", name="review", arguments={})]
            )
        elif number == 3:
            assert not outputs(body)[-1]["ok"]
            assert not any(item.startswith("get:") for item in calls(tmp_path))
            response = tool_response(
                [call("get", "mcp_templates_get_prompt", name="review", arguments={"text": "code"})]
            )
        else:
            value = outputs(body)[-1]
            assert value["ok"] and value["usage"] == "reference_only"
            assert "external template:" in value["messages"][0]["text"]
            response = answer("Used template as reference")
        return httpx.Response(200, json=response)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, _):
        assert recorder.run(lambda: app.send("task")) == "Used template as reference"
        events = store.load_events(app.session.session_id)
        assert len([e for e in events if e["type"] == "tool_started"]) == 3
    assert calls(tmp_path).count("get:review") == 1
    assert_stopped(int((tmp_path / "server.pid").read_text()))
    forbid_external_actions(monkeypatch)
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


def test_prompt_adapter_rejects_existing_names(tmp_path):
    client = MCPConnection(server_at(tmp_path), tmp_path).open()
    try:
        registry = register_prompts(create_tool_registry(tmp_path), client)
        with pytest.raises(ToolError, match="冲突"):
            register_prompts(registry, client)
    finally:
        client.close()


def test_teaching_service_renders_named_prompt_without_reading_files(tmp_path):
    example = Path(__file__).resolve().parents[2] / "examples/mcp_readonly.py"
    config = MCPServerConfig(
        "demo",
        sys.executable,
        (str(example), "--workspace", str(tmp_path)),
        prompts=("review_text",),
    )
    client = MCPConnection(config, tmp_path).open()
    try:
        result = client.get_prompt("review_text", {"text": "hello", "focus": "clarity"})
        assert result["messages"] == [
            {
                "role": "user",
                "text": "Review the supplied text for clarity. Cite evidence and uncertainty.\n\nhello",
            }
        ]
    finally:
        client.close()


@pytest.mark.parametrize("prompts", [["bad name"], ["review", "review"], ["x" * 41], "review"])
def test_prompt_config_rejects_ambiguous_names(prompts):
    with pytest.raises(ConfigError):
        parse_mcp({"servers": [{"alias": "templates", "command": "python", "prompts": prompts}]})


def test_prompt_only_configuration_stays_disabled_by_default(tmp_path, monkeypatch):
    value = parse_mcp({"servers": [{"alias": "demo", "command": "python", "prompts": ["review"]}]})
    assert not value.enabled and value.servers[0].tools == value.servers[0].resources == ()
    monkeypatch.setattr(MCPConnection, "open", lambda *_: pytest.fail("disabled server started"))
    config = replace(config_at(tmp_path), mcp=value)
    with captured_app(
        tmp_path, lambda *_: httpx.Response(200, json=answer("ok")), config=config
    ) as (app, recorder, _, _, _):
        assert recorder.run(lambda: app.send("task")) == "ok"

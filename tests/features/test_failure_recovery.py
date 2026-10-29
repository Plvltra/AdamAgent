"""V5.4：失败后用日志解释可继续边界，重开会话仍执行相同规则。"""

import hashlib
import json
import sys

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import chat
from adam.config.settings import AgentConfig, AppConfig, LLMConfig
from adam.contracts.errors import AgentError, BudgetError, LLMError, StorageError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore


@pytest.mark.parametrize("mode", ["known", "unknown", "cap"])
def test_reopened_failed_session_preserves_continuation_rules(tmp_path, mode):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        payload = {
            "choices": [
                {
                    "finish_reason": "length" if len(requests) == 1 else "stop",
                    "message": {"role": "assistant", "content": "done"},
                }
            ]
        }
        if mode != "unknown" or len(requests) > 1:
            payload["usage"] = {"prompt_tokens": 4, "completion_tokens": 2}
        return httpx.Response(200, json=payload)

    config = AppConfig(
        LLMConfig("https://example.test", "test", "key", stream=False),
        tmp_path / "sessions",
        "",
        agent=AgentConfig(max_session_tokens=6 if mode == "cap" else 50000),
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = LLMClient(config.llm, http)
        with SessionStore(config.sessions_dir) as store:
            app = build_application(config, store, client, tmp_path)
            with pytest.raises(LLMError):
                app.send("failed input")
            session_id = app.session.session_id
            status = app.continuation_status()
            assert status.allowed is (mode == "known")
            assert (
                status.reason
                == {"known": "ready", "unknown": "usage_unknown", "cap": "session_token_limit"}[
                    mode
                ]
            )
            assert app.history() == []
        with SessionStore(config.sessions_dir) as reopened:
            app = build_application(config, reopened, client, tmp_path)
            app.resume(session_id)
            assert app.continuation_status() == status
            if mode == "known":
                assert app.send("next input") == "done"
                assert all(
                    item.get("content") != "failed input" for item in requests[-1]["messages"]
                )
            else:
                with pytest.raises(BudgetError):
                    app.send("next input")
                assert len(requests) == 1
            report = read_report(next(config.sessions_dir.rglob("*.jsonl")))
            assert report["turns"][0]["user_text"] == "failed input"
            assert report["turns"][0]["requests"][0]["reason"] == "invalid_response"


@pytest.mark.parametrize("operation", ["command", "edit"])
def test_unknown_action_blocks_chat_and_manual_compaction(tmp_path, monkeypatch, operation):
    requests = []
    target = tmp_path / "source.txt"
    target.write_bytes(b"old")
    name, args = (
        ("run_command", {"argv": [sys.executable, "-c", "pass"]})
        if operation == "command"
        else (
            "replace_text",
            {
                "path": "source.txt",
                "old_text": "old",
                "new_text": "new",
                "expected_version": hashlib.sha256(b"old").hexdigest(),
            },
        )
    )

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "cmd",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    config = AppConfig(
        LLMConfig("https://example.test", "test", "key", stream=False), tmp_path / "sessions", ""
    )
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            tmp_path,
            confirm_command=lambda _: True,
            confirm_edit=lambda _: True,
        )
        method = "append_command_event" if operation == "command" else "append_edit_event"
        original = getattr(store, method)

        def fail(session_id, kind, payload):
            if kind == f"{operation}_finished":
                raise StorageError("injected result write failure")
            original(session_id, kind, payload)

        monkeypatch.setattr(store, method, fail)
        with pytest.raises(AgentError):
            app.send("run once")
        assert app.continuation_status().reason == "execution_unknown"
        with pytest.raises(StorageError):
            app.send("continue")
        with pytest.raises(StorageError):
            app.compact()
        assert len(requests) == 1
        operations = store.load_executions(app.session.session_id) + store.load_edits(
            app.session.session_id
        )
        assert len(operations) == 1
        assert target.read_bytes() == (b"new" if operation == "edit" else b"old")


def test_cli_guides_unknown_usage_to_new_session(tmp_path):
    output, requests = [], []
    inputs = iter(["fail", "/usage", "/new next", "succeed", "/exit"])

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    config = AppConfig(
        LLMConfig("https://example.test", "test", "key", stream=False), tmp_path / "sessions", ""
    )
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        chat(app, read=lambda _: next(inputs), write=output.append)
        assert "用量未知，原会话只供查看；用 /new" in "\n".join(output)
        assert len(requests) == 2 and len(app.list_sessions()) == 2
        assert app.history()[-1].content == "done"

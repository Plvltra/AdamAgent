"""聊天业务：多轮上下文、模型切换、终端指令与退出。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from adam.cli import chat as run_chat
from adam.config import LLMConfig
from adam.llm import LLMClient
from adam.storage import SessionStore


def chat(client, system_prompt, **kwargs):
    with TemporaryDirectory() as directory, SessionStore(Path(directory)) as store:
        run_chat(client, system_prompt, store, **kwargs)


def response(text="hello"):
    return {
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    }


def test_multiturn_failure_and_reset():
    requests = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        requests.append(json.loads(request.content))
        assert request.url == "https://example.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        if len(requests) == 2:
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": []}
            )
        return httpx.Response(200, json=response(f"reply{len(requests)}"))

    entries = iter([" ", "one", "failed", "two", "/new", "three", "/exit"])
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("https://example.test/v1", "test-model", "secret"), http)
        chat(client, "system", read=lambda _: next(entries), write=output.append)
    assert requests[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "one"},
    ]
    assert requests[2]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "two"},
    ]
    assert requests[3]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "three"},
    ]
    assert all(r["stream"] is False for r in requests)
    assert "secret" not in "".join(output)
    assert any("响应格式" in line for line in output)


@pytest.mark.parametrize("signal", [EOFError, KeyboardInterrupt])
def test_exit_signal(signal):
    def read(_):
        raise signal

    output = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as http:
        client = LLMClient(LLMConfig("https://example.test", "model", "secret"), http)
        chat(client, "", read=read, write=output.append)
    assert output[-1] == "再见。"


def test_model_switch_preserves_history_and_rejects_unknown():
    requests = []
    config = LLMConfig("https://example.test/v1", "flash", "secret")

    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret"
        if request.method == "GET":
            assert request.url == "https://example.test/v1/models"
            return httpx.Response(
                200, json={"data": [{"id": "flash", "name": "Example V4 Flash"}, {"id": "pro"}]}
            )
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "pro":
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": []}
            )
        return httpx.Response(200, json=response())

    entries = iter(
        ["one", "/model", "/model unknown", "two", "/model pro", "failed", "again", "/exit"]
    )
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        chat(LLMClient(config, http), "system", read=lambda _: next(entries), write=output.append)
    assert [r["model"] for r in requests] == ["flash", "flash", "pro", "pro"]
    assert requests[2]["messages"][:-1] == requests[3]["messages"][:-1]
    assert requests[2]["messages"][1]["content"] == "one"
    assert len(requests[2]["messages"]) == 6
    assert config.model == "flash"
    assert any("Example V4 Flash" in line for line in output)
    assert any("未切换" in line for line in output)
    assert "请求 ID：" not in "".join(output)
    assert "flash" not in "".join(output)
    assert "pro" not in "".join(output)
    assert any("具体版本未知" in line for line in output)


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
def test_model_catalog_failure_still_allows_switch_and_chat(failure):
    requests = []

    def handler(request):
        if request.method == "GET":
            if failure == "timeout":
                raise httpx.ReadTimeout("secret", request=request)
            if failure == "malformed":
                return httpx.Response(200, json={"data": [None]})
            return httpx.Response(404, text="secret")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    entries = iter(["/model custom-model", "hello", "/exit"])
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("https://example.test", "flash", "secret"), http)
        chat(client, "", read=lambda _: next(entries), write=output.append)
    assert len(requests) == 1
    assert requests[0]["model"] == "custom-model"
    assert any("尚未验证" in line for line in output)
    assert "secret" not in "".join(output)


def test_commands_do_not_reach_model_and_exit_stops_input():
    requests = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    entries = iter(["/unknown", "/exit extra", "/model", "hello", "/exit", "unread"])
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("https://example.test", "model", "secret"), http)
        chat(client, "", read=lambda _: next(entries), write=output.append)
    assert len(requests) == 1
    assert requests[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert sum("未知命令" in line for line in output) == 2
    assert next(entries) == "unread"
    assert output[-1] == "再见。"

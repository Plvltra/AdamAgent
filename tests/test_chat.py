import json

import httpx
import pytest

from adam.cli import chat
from adam.config import Config, ConfigError, load_config
from adam.llm import LLMClient, LLMError


def response(text="hello"):
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]
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
            return httpx.Response(503, text="secret remote body")
        return httpx.Response(200, json=response(f"reply{len(requests)}"))

    entries = iter([" ", "one", "failed", "two", "/new", "three", "/exit"])
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(Config("https://example.test/v1", "test-model", "secret"), http)
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
    assert any("503" in line for line in output)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        response(""),
        {
            "choices": [
                {"finish_reason": "length", "message": {"role": "assistant", "content": "partial"}}
            ]
        },
    ],
)
def test_invalid_responses(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(LLMError):
            LLMClient(Config("https://example.test", "model", "secret"), http).complete([])


def test_timeout_is_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMError, match="超时") as error:
            LLMClient(Config("https://example.test", "model", "secret"), http).complete([])
    assert count == 1
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("signal", [EOFError, KeyboardInterrupt])
def test_exit_signal(signal):
    def read(_):
        raise signal

    output = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as http:
        client = LLMClient(Config("https://example.test", "model", "secret"), http)
        chat(client, "", read=read, write=output.append)
    assert output[-1] == "再见。"


BASE = '[llm]\nbase_url = "https://example.test/v1/"\nmodel = "test"\n'


def test_config_modes(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE + 'api_key_env = "ADAM_TEST_KEY"\n', encoding="utf-8-sig")
    monkeypatch.setenv("ADAM_TEST_KEY", "environment-secret")
    config = load_config(path)
    assert config.api_key == "environment-secret"
    assert config.base_url == "https://example.test/v1"
    assert "environment-secret" not in repr(config)
    path.write_text(BASE + 'api_key = "literal-secret"\n', encoding="utf-8")
    assert load_config(path).api_key == "literal-secret"


@pytest.mark.parametrize(
    "extra",
    [
        "",
        'api_key = ""',
        'api_key = "secret"\napi_key_env = "TEST"',
        'api_key_env = "ADAM_MISSING_KEY"',
        'api_key = "secret"\ntimeout_seconds = -1',
        'api_key = "secret"\ntimeout_seconds = true',
        'api_key = "secret"\ntimeout_seconds = nan',
        'api_key = "secret"\nsystem_prompt = 42',
        'api_key = "secret"\ntimeout = 30',
    ],
)
def test_config_errors(tmp_path, monkeypatch, extra):
    monkeypatch.delenv("ADAM_MISSING_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(BASE + extra, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_malformed_toml_does_not_expose_secret(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BASE + 'api_key = "secret', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret" not in str(error.value)


def test_model_switch_preserves_history_and_rejects_unknown():
    requests = []
    config = Config("https://example.test/v1", "flash", "secret")

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
            return httpx.Response(503)
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
        client = LLMClient(Config("https://example.test", "flash", "secret"), http)
        chat(client, "", read=lambda _: next(entries), write=output.append)
    assert len(requests) == 1
    assert requests[0]["model"] == "custom-model"
    assert any("尚未验证" in line for line in output)
    assert "secret" not in "".join(output)

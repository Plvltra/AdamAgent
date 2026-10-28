"""模型接口的响应解析、网络错误与非流式协议校验。"""

import json

import httpx
import pytest

from adam.config import LLMConfig
from adam.llm import LLMClient, LLMError


def response(text="hello"):
    return {
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    }


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
            LLMClient(
                LLMConfig("https://example.test", "model", "secret", stream=False), http
            ).complete([])


def test_timeout_is_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMError, match="超时") as error:
            LLMClient(
                LLMConfig("https://example.test", "model", "secret", stream=False), http
            ).complete([])
    assert count == 1
    assert "secret" not in str(error.value)


def test_unexpected_stream_response_has_clear_error():
    def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            text='data: {"private": "secret-response"}\n\ndata: [DONE]\n\n',
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("https://example.test", "model", "secret", stream=False), http)
        with pytest.raises(LLMError, match="流式响应") as error:
            client.complete([])
    assert "secret" not in str(error.value)

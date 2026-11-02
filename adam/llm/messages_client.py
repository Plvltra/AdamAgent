"""Messages最小非流式HTTP适配；失败不切换协议或自动重发。"""

from typing import Any

import httpx

from adam.config.settings import LLMConfig
from adam.contracts.errors import FailureCode, LLMError, RequestNotSentError
from adam.contracts.models import Completion
from adam.llm.messages_protocol import decode_message, encode_messages
from adam.llm.messages_usage import parse_messages_usage


def complete_messages(config: LLMConfig, client: httpx.Client, body: dict[str, Any]) -> Completion:
    """转换后发送一次请求；不兼容历史在联网前拒绝，协议失败保留已知用量。"""
    try:
        payload = encode_messages(body["messages"])
        payload.update(
            model=body["model"],
            max_tokens=body["max_tokens"],
            stream=False,
            thinking={"type": "disabled"},
        )
        if body.get("tools"):
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"],
                }
                for tool in body["tools"]
            ]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LLMError(
            "历史或工具不能转换为Messages；请求未发送，请新建会话。",
            (0, 0),
            code=FailureCode.INVALID_RESPONSE,
        ) from None
    try:
        response = client.post(
            config.base_url.rstrip("/") + "/v1/messages",
            headers={"x-api-key": config.api_key, "anthropic-version": "2023-06-01"},
            json=payload,
            timeout=config.timeout_seconds,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise RequestNotSentError() from None
    except httpx.TimeoutException:
        raise LLMError("Messages请求超时，未自动重试。", code=FailureCode.API_TIMEOUT) from None
    except httpx.RequestError:
        raise LLMError("Messages连接失败，用量未知。", code=FailureCode.API_CONNECTION) from None
    if not 200 <= response.status_code < 300:
        raise LLMError(
            f"HTTP {response.status_code}：Messages请求失败。",
            code=FailureCode.API_HTTP,
            http_status=response.status_code,
        )
    usage = None
    try:
        data = response.json()
        usage = parse_messages_usage(data.get("usage"))
        return Completion(decode_message(data), usage)
    except (ValueError, KeyError, TypeError, AttributeError):
        # 不回显响应原文，避免泄漏服务端返回的凭据或用户内容。
        raise LLMError(
            "Messages响应无效或未完整结束。", usage, code=FailureCode.INVALID_RESPONSE
        ) from None

"""Synchronous, non-streaming Chat Completions API transport."""

from typing import Any

import httpx

from adam.errors import FailureCode, LLMError
from adam.models import Completion
from adam.protocol import validate_assistant
from adam.settings import LLMConfig


class LLMClient:
    def __init__(self, config: LLMConfig, client: httpx.Client) -> None:
        self.config = config
        self.client = client

    def list_models(self) -> dict[str, str | None]:
        try:
            response = self.client.get(
                self.config.base_url + "/models",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=min(self.config.timeout_seconds, 5),
            )
        except httpx.RequestError:
            raise LLMError("模型目录暂时无法获取。") from None

        if not 200 <= response.status_code < 300:
            raise LLMError("模型目录暂时无法获取。")

        try:
            data = response.json()["data"]
            if not isinstance(data, list):
                raise ValueError

            models: dict[str, str | None] = {}
            for item in data:
                model_id = item["id"]
                if not isinstance(model_id, str) or not model_id.strip():
                    raise ValueError

                name = item.get("name")
                models[model_id] = name.strip() if isinstance(name, str) and name.strip() else None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LLMError("模型目录格式无效。") from None

        return models

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> Completion:
        """发送一次非流式请求，返回助手消息及服务端报告的 token 用量。

        Args:
            messages: 按协议排列的上下文，包含已有工具结果和必要的推理字段。
            model: 本次模型覆盖值；None 使用 LLMConfig 中的默认模型。
            tools: 本次允许模型请求的工具说明；本方法不执行工具。
            max_tokens: 本次输出 token 上限。

        Returns:
            Completion：普通回答或工具请求；usage 为输入/输出 token 数，
            服务端未报告可靠用量时为 None。

        Raises:
            LLMError: 网络、HTTP 或协议错误；携带受控原因码、HTTP 状态及已解析用量。

        协议参考：https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
        """
        body: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            # 一次返回完整 JSON；流式响应需要单独的增量解析逻辑。
            "stream": False,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        try:
            response = self.client.post(
                self.config.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=body,
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LLMError(
                "请求超时；用量未知，未自动重试。", code=FailureCode.API_TIMEOUT
            ) from None
        except httpx.RequestError:
            raise LLMError("网络连接失败；用量未知。", code=FailureCode.API_CONNECTION) from None
        # 不回显远端响应正文或原始异常，其中可能包含凭据。
        if not 200 <= response.status_code < 300:
            raise LLMError(
                f"HTTP {response.status_code}：请求失败；用量未知，未自动重试。",
                code=FailureCode.API_HTTP,
                http_status=response.status_code,
            )
        if (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            == "text/event-stream"
        ):
            raise LLMError(
                "服务返回了流式响应，但当前客户端只支持非流式 JSON 回答。",
                code=FailureCode.INVALID_RESPONSE,
            )
        usage = None
        try:
            data = response.json()
            raw_usage = data.get("usage", {})
            if isinstance(raw_usage, dict):
                inputs, outputs = raw_usage.get("prompt_tokens"), raw_usage.get("completion_tokens")
                if type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
                    usage = (inputs, outputs)
            # choices 是候选回答列表；当前只使用第一个候选。
            choice = data["choices"][0]
            message = choice["message"]
            validate_assistant(message)
            # 工具请求允许 content=None；length 等截断状态不能作为完整回答。
            expected = "tool_calls" if message.get("tool_calls") else "stop"
            if choice.get("finish_reason") != expected:
                raise ValueError("unfinished response")
            # 保留模型实际返回的推理字段，供后续工具请求按服务商协议回传。
            clean = {
                key: message[key]
                for key in ("role", "content", "tool_calls", "reasoning_content")
                if key in message
            }
            return Completion(clean, usage)
        # JSON、字段和类型错误统一转换，但保留已经解析出的用量。
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(
                "模型响应格式无效或回答未完整结束；本轮不加入历史。",
                usage,
                code=FailureCode.INVALID_RESPONSE,
            ) from None

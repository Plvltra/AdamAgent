"""Chat Completions 传输适配器；支持完整 JSON 与有界 SSE 增量解析。"""

from collections.abc import Callable
from typing import Any

import httpx

from adam.errors import FailureCode, LLMError, RequestInterrupted
from adam.models import Completion, ModelOutput
from adam.protocol import validate_assistant
from adam.settings import LLMConfig
from adam.streaming import StreamReply, parse_usage, sse_events


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
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """发送一次请求，返回完整助手消息及服务端报告的 token 用量。

        Args:
            messages: 按协议排列的上下文，包含已有工具结果和必要的推理字段。
            model: 本次模型覆盖值；None 使用 LLMConfig 中的默认模型。
            tools: 本次允许模型请求的工具说明；本方法不执行工具。
            max_tokens: 本次输出 token 上限。
            on_output: 展示正文增量与响应结束边界；不通知推理内容，摘要可不传。

        Returns:
            Completion：普通回答或工具请求；usage 为输入/输出 token 数，
            服务端未报告可靠用量时为 None。

        Raises:
            LLMError: 网络、HTTP 或协议错误；携带受控原因码、HTTP 状态及已解析用量。
            RequestInterrupted: 流式请求被取消，携带中断前已收到的可靠用量。

        协议参考：https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
        """
        body: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "stream": self.config.stream,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        if self.config.stream:
            body["stream_options"] = {"include_usage": True}
            return self._stream(body, on_output)
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
                "服务返回了流式响应，但配置要求非流式 JSON；请检查 llm.stream。",
                code=FailureCode.INVALID_RESPONSE,
            )
        usage = None
        try:
            data = response.json()
            usage = parse_usage(data.get("usage"))
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

    def _stream(
        self, body: dict[str, Any], on_output: Callable[[ModelOutput], None] | None
    ) -> Completion:
        """收齐 SSE 后才交回响应；断流保留已知用量，部分文本不持久化或执行。"""
        reply = StreamReply()
        try:
            with self.client.stream(
                "POST",
                self.config.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=body,
                timeout=self.config.timeout_seconds,
            ) as response:
                self._check_stream_response(response)
                events = iter(sse_events(response.iter_bytes()))
                while True:
                    # 只转换协议解析错误；界面回调失败仍按原异常传播。
                    try:
                        event = next(events)
                        if event == "[DONE]":
                            return reply.finish()
                        text = reply.accept(event)
                    except (
                        StopIteration,
                        ValueError,
                        KeyError,
                        IndexError,
                        TypeError,
                        AttributeError,
                    ):
                        raise LLMError(
                            "流式响应格式无效或未完整结束；本轮不加入历史。",
                            reply.usage,
                            code=FailureCode.INVALID_RESPONSE,
                        ) from None
                    if text and on_output:
                        on_output(ModelOutput("text", text))
        except httpx.TimeoutException:
            raise LLMError(
                "请求超时；未自动重试。", reply.usage, code=FailureCode.API_TIMEOUT
            ) from None
        except httpx.RequestError:
            raise LLMError(
                "网络连接或流式读取失败；未自动重试。", reply.usage, code=FailureCode.API_CONNECTION
            ) from None
        except KeyboardInterrupt:
            raise RequestInterrupted(reply.usage) from None
        finally:
            if on_output:
                on_output(ModelOutput("response_end"))

    @staticmethod
    def _check_stream_response(response: httpx.Response) -> None:
        """拒绝不匹配的协议并提示显式切换；不会另发一次可能收费的请求。"""
        if not 200 <= response.status_code < 300:
            raise LLMError(
                f"HTTP {response.status_code}：流式请求失败；服务不支持时可设置 llm.stream=false。",
                code=FailureCode.API_HTTP,
                http_status=response.status_code,
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/event-stream":
            raise LLMError(
                "服务未返回 SSE；可设置 llm.stream=false 后手动重试。",
                code=FailureCode.INVALID_RESPONSE,
            )

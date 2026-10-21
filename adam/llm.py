"""Synchronous, non-streaming Chat Completions API transport."""

import httpx

from adam.config import LLMConfig


class LLMError(RuntimeError):
    pass


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

    def complete(self, messages: list[dict[str, str]], *, model: str | None = None) -> str:
        """发送一次非流式请求，返回本项目支持的完整文本回答。

        DeepSeek 请求字段与响应结构：
        https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
        阅读 Request Body 和 Responses 中的 200 (No streaming) 部分。
        """
        try:
            response = self.client.post(
                self.config.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json={
                    "model": model if model is not None else self.config.model,
                    "messages": messages,
                    # 一次返回完整 JSON；流式响应需要另一套增量解析逻辑。
                    "stream": False,
                },
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LLMError("请求超时；未自动重试，可重新输入。") from None
        except httpx.RequestError:
            raise LLMError("网络连接失败，请检查网络、代理和 base_url。") from None

        # Do not echo remote bodies or exception strings: they may contain credentials.
        if not 200 <= response.status_code < 300:
            hints = {401: "鉴权失败，请检查密钥", 403: "访问被拒绝", 429: "请求受限，请稍后再试"}
            hint = hints.get(response.status_code, "服务返回错误，请检查配置或稍后重试")
            raise LLMError(f"HTTP {response.status_code}：{hint}。")

        try:
            # choices 是候选回答列表；当前只读取第一项的 message.content。
            choice = response.json()["choices"][0]
            message = choice["message"]
            content = message["content"]

            # stop 表示自然结束或命中停止序列；length 等原因可能意味着截断。
            # tool_calls 是工具调用请求。它可以是合法响应，但当前项目尚不支持。
            if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
                raise LLMError("模型未返回完整文本回答；本轮不加入历史。")

            # API 允许 content 为 null；本项目只接受 assistant 的非空文本。
            # 这是当前功能范围的限制，不代表其他响应形式违反 API 协议。
            if (
                message.get("role") != "assistant"
                or not isinstance(content, str)
                or not content.strip()
            ):
                raise LLMError("模型返回空回答或不支持的消息格式。")
        # 分别兜底 JSON 解码失败、字段缺失、空列表以及字段类型不符。
        # 上面主动抛出的 LLMError 不属于这些类型，会原样交给 CLI 处理。
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("API 响应格式不符合 Chat Completions 协议。") from None

        return content

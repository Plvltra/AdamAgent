"""业务流程需要的模型能力；HTTP 客户端和内存假模型均可实现。"""

from collections.abc import Callable
from typing import Any, Protocol

from adam.contracts.models import Completion, ModelOutput


class ModelClient(Protocol):
    """提供模型目录和单次响应；不要求暴露连接配置、密钥或 HTTP 会话。"""

    def list_models(self) -> dict[str, str | None]:
        """返回模型 ID 与可选显示名称；服务不可用时抛出 LLMError。"""
        ...

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """取得一次模型响应；失败时抛出携带已知用量的 LLMError。"""
        ...

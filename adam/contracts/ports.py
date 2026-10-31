"""应用使用的模型、工具与技能接口；适配器各自实现，不向界面暴露底层配置。"""

from collections.abc import Callable
from typing import Any, Protocol

from adam.contracts.execution import ToolBudget
from adam.contracts.models import Completion, ModelOutput
from adam.contracts.skills import SkillListing, SkillSelection


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


class ToolRunner(Protocol):
    """提供工具说明并执行有来源的批次；允许在不改主循环时装饰录制或离线结果。"""

    def definitions(self) -> list[dict[str, Any]]:
        """返回实际工具说明，不执行工具。"""
        ...

    def execute(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
        budget: ToolBudget,
    ) -> list[dict[str, Any]]:
        """沿用共享编辑额度，返回按调用 ID 配对的工具消息；失败保持原异常。"""
        ...


class SkillProvider(Protocol):
    """应用仅查看目录或选择本轮指导；具体发现、权限和文件读取由加载器负责。"""

    def listings(self) -> tuple[SkillListing, ...]:
        """取得包含覆盖来源的只读目录，不调用模型。"""
        ...

    def select(self, skill_id: str) -> SkillSelection:
        """读取生效正文快照；不存在、来源变化或读取受限时抛 AgentError。"""
        ...

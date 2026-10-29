"""面向界面的应用操作；隐藏存储访问与模型目录策略，沿用公共错误契约。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.application.conversation import Conversation
from adam.application.experiences import ExperienceService
from adam.application.memories import MemoryService
from adam.context.builder import RequestContext
from adam.contracts.errors import AgentError, LLMError
from adam.contracts.history import Summary
from adam.contracts.memory import MemoryCommand, MemoryRecord
from adam.contracts.models import (
    ContinuationStatus,
    ExecutionReport,
    Message,
    ModelOutput,
    Session,
    SessionUsage,
)
from adam.contracts.planning import PlanReport
from adam.contracts.ports import ModelClient
from adam.session.storage import SessionStore


class AgentApplication:
    """提供会话、模型选择和聊天入口；依赖由组装模块注入，资源也由其关闭。"""

    def __init__(
        self,
        conversation: Conversation,
        store: SessionStore,
        models: ModelClient,
        memories: MemoryService | None = None,
        *,
        close_resources: Callable[[], None] | None = None,
    ) -> None:
        self._close_resources = close_resources
        self._conversation = conversation
        self._store = store
        self._models_client = models
        self._memories = memories
        self._experiences = ExperienceService(store, memories) if memories else None
        self._models: dict[str, str | None] | None = None

    @property
    def session(self) -> Session | None:
        """当前选中的会话；仅查看不会创建空会话。"""
        return self._conversation.session

    @property
    def model(self) -> str:
        """后续模型请求使用的 ID；切换仅在本次运行生效。"""
        return self._conversation.model

    @property
    def sessions_dir(self) -> Path:
        """实际使用的会话目录，供界面展示。"""
        return self._store.root

    @property
    def models(self) -> dict[str, str | None] | None:
        """已查询的模型目录副本；None 表示尚未取得可靠目录。"""
        return dict(self._models) if self._models is not None else None

    def refresh_models(self) -> bool:
        """刷新可选模型；目录服务失败仍可聊天，并允许用户提供模型 ID。"""
        try:
            self._models = self._models_client.list_models()
        except LLMError:
            self._models = None
            return False
        return True

    def select_model(self, model: str) -> None:
        """校验并切换后续模型，不修改配置或历史；已知目录不含该 ID 时保持原值。"""
        if self._models is not None and model not in self._models:
            raise AgentError("模型 ID 不在服务端目录中，未切换；请使用服务商支持的 API 模型 ID。")
        self._conversation.model = model

    def new(self, title: str = "新对话") -> Session:
        """创建并切换到新会话，原会话保留。"""
        return self._conversation.new(title)

    def resume(self, session_id: str) -> Session:
        """恢复已保存会话；切换失败时保留原会话。"""
        return self._conversation.resume(session_id)

    def list_sessions(self) -> list[Session]:
        """列出已有会话，不改变当前选择。"""
        return self._store.list_sessions()

    def history(self) -> list[Message]:
        """取得当前会话的完整成功历史；无会话时返回空列表。"""
        session = self.session
        return self._store.load_messages(session.session_id) if session else []

    def manage_memory(self, command: MemoryCommand) -> list[MemoryRecord]:
        """执行用户明确的记忆管理；不发起模型请求，也不隐式创建会话。"""
        if self._memories is None:
            raise AgentError("当前应用未配置记忆管理。")
        return self._memories.manage(command, self.session.session_id if self.session else None)

    def experience_candidates(self) -> list[dict[str, Any]]:
        """查询当前会话候选；不创建会话，不发模型请求。"""
        if self.session is None or self._experiences is None:
            return []
        return self._experiences.candidates(self.session.session_id)

    def decide_experience(
        self, candidate_id: str, action: str, content: str = ""
    ) -> MemoryRecord | None:
        """执行用户明确的候选决定；只有接受才写入当前项目记忆。"""
        if self.session is None or self._experiences is None:
            raise AgentError("没有可处理经验的当前会话。")
        return self._experiences.decide(self.session.session_id, candidate_id, action, content)

    def usage(self) -> SessionUsage:
        """查询当前会话消费；缺失用量保持未知，不触发模型请求。"""
        session = self.session
        total, unknown = self._conversation.usage() if session else (0, False)
        return SessionUsage(total, self._conversation.limits.max_session_tokens, unknown)

    def continuation_status(self) -> ContinuationStatus:
        """只读判断原会话能否继续请求；未知消费和未知副作用不以清零或重放修复。"""
        if self.session is None:
            return ContinuationStatus(True, "no_session", "下一条输入会新建会话。")
        operations = self._store.load_executions(self.session.session_id) + self._store.load_edits(
            self.session.session_id
        )
        if any(item["status"] == "unknown" for item in operations):
            return ContinuationStatus(
                False,
                "execution_unknown",
                "执行结果未知；先核对实际副作用和日志，再用 /new 开新会话；不会自动重跑。",
            )
        usage = self.usage()
        if usage.unknown:
            return ContinuationStatus(
                False,
                "usage_unknown",
                "用量未知，原会话只供查看；用 /new 开新会话，或 /resume 切换，不能把未知消费清零。",
            )
        if usage.tokens >= usage.limit:
            return ContinuationStatus(
                False,
                "session_token_limit",
                "当前会话消费已达上限；可 /new，或调整额度配置后重启并 /resume。",
            )
        return ContinuationStatus(
            True,
            "ready",
            "可调整输入后继续本会话；失败输入未加入有效历史，已执行的工具操作保留。",
        )

    def send(self, text: str, *, on_output: Callable[[ModelOutput], None] | None = None) -> str:
        """执行完整一轮并返回已保存回答；已知失败以 AgentError 交给界面。"""
        return (
            self._conversation.send(text, on_output=on_output)
            if on_output
            else self._conversation.send(text)
        )

    def close(self) -> None:
        """释放组装时注册的子任务与服务资源；外部注入的主存储和模型由调用方关闭。"""
        if self._close_resources:
            self._close_resources()
        elif self._conversation.delegation:
            self._conversation.delegation.close()

    def execution_report(self) -> ExecutionReport | None:
        """读取本次运行最近轮次的执行事实；切换会话后为空，不解释或重放底层操作。"""
        return self._conversation.execution_report()

    def plan_report(self) -> PlanReport | None:
        """查询当前会话最近的计划及轮次状态；无会话时不创建数据，不调用模型。"""
        if self.session is None:
            return None
        return self._store.load_plan(self.session.session_id)

    def compact(self) -> Summary | None:
        """生成并保存旧历史摘要，不修改原始记录。"""
        return self._conversation.compact()

    def preview_context(self, query: str = "") -> RequestContext:
        """查看基础上下文；可提供假设问题预览相关记忆，不提交消息或请求模型。"""
        return self._conversation.preview_context(query)

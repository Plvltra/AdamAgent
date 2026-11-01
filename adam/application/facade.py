"""面向界面的应用操作；隐藏存储访问与模型目录策略，沿用公共错误契约。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from adam.application.conversation import Conversation
from adam.application.experiences import ExperienceService
from adam.application.memories import MemoryService
from adam.application.modes import ModeController
from adam.application.running_inputs import RunningInputs
from adam.application.undo import UndoService
from adam.config.roles import RoleProfile
from adam.context.builder import RequestContext
from adam.contracts.checkpoints import UndoResult
from adam.contracts.errors import AgentError, LLMError
from adam.contracts.events import EventReceipt
from adam.contracts.execution import ExecutionMode
from adam.contracts.history import Summary
from adam.contracts.memory import MemoryCommand, MemoryRecord
from adam.contracts.memory_organization import MemoryOrganization
from adam.contracts.models import (
    ContinuationStatus,
    ExecutionReport,
    Message,
    ModelOutput,
    Session,
    SessionUsage,
)
from adam.contracts.planning import PlanReport
from adam.contracts.ports import ModelClient, SkillProvider
from adam.contracts.skills import SkillListing
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
        on_interrupt: Callable[[], None] | None = None,
        running_inputs: RunningInputs | None = None,
        on_session_selected: Callable[[str], None] | None = None,
        skills: SkillProvider | None = None,
        modes: ModeController | None = None,
        undo: UndoService | None = None,
        role: RoleProfile | None = None,
        handoff_boundary: tuple[str, ...] = (),
    ) -> None:
        self._close_resources = close_resources
        self._running_inputs = running_inputs
        self._on_interrupt, self._on_session_selected = on_interrupt, on_session_selected
        self._conversation = conversation
        self._store = store
        self._models_client = models
        self._memories = memories
        self._skills = skills
        self._modes = modes
        self._undo = undo
        self.role = role
        self.handoff_boundary = handoff_boundary
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

    @property
    def mode(self) -> ExecutionMode:
        """当前终端模式；旧的固定执行入口保持 act，不据旧会话恢复模式。"""
        return self._modes.current() if self._modes is not None else ExecutionMode.ACT

    def select_mode(self, mode: str) -> None:
        """执行用户明确的切换，不请求模型、不创建会话；未绑定策略的入口拒绝切换。"""
        if self._modes is None:
            raise AgentError("当前应用没有绑定模式策略，不能切换；请使用标准启动入口。")
        self._modes.select(mode)

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
        if self.role and model != self.role.model:
            raise AgentError("角色模型已由宿主固定，不能在会话中扩大模型范围。")
        if self._models is not None and model not in self._models:
            raise AgentError("模型 ID 不在服务端目录中，未切换；请使用服务商支持的 API 模型 ID。")
        self._conversation.model = model

    def new(self, title: str = "新对话") -> Session:
        """创建并切换到新会话，原会话保留。"""
        return self._conversation.new(title)

    def resume(self, session_id: str) -> Session:
        """恢复已保存会话；主会话切换失败时保留原会话。

        切换成功后恢复附属任务；附属记录故障会报错，但不回滚已选中的会话。
        """
        session = self._conversation.resume(session_id)
        if self._on_session_selected:
            self._on_session_selected(session_id)
        return session

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

    def organize_memory(
        self, identity: str | None = None, *, global_scope: bool = False
    ) -> MemoryOrganization:
        """生成或查看明确范围内的整理候选；不开始聊天轮次或调用模型。"""
        if self._memories is None:
            raise AgentError("当前应用未配置记忆管理。")
        return self._memories.organize(identity, global_scope=global_scope)

    def accept_memory_organization(
        self,
        identity: str,
        candidate_id: str,
        *,
        global_scope: bool = False,
        content: str | None = None,
    ) -> MemoryRecord:
        """执行用户对一个整理候选的明确确认，不调用模型或创建会话。"""
        if self._memories is None:
            raise AgentError("当前应用未配置记忆管理。")
        return self._memories.accept_organization(
            identity,
            candidate_id,
            global_scope=global_scope,
            content=content,
            session_id=self.session.session_id if self.session else None,
        )

    def reject_memory_organization(
        self, identity: str, *, global_scope: bool = False
    ) -> MemoryOrganization:
        """执行用户对整份候选的拒绝，不改原记忆或发起模型请求。"""
        if self._memories is None:
            raise AgentError("当前应用未配置记忆管理。")
        return self._memories.reject_organization(identity, global_scope=global_scope)

    def undo_memory_organization(
        self, memory_id: str, revision: int, *, global_scope: bool = False
    ) -> MemoryRecord:
        """撤销未改动概览，返回已追加的撤销记录；故障不提前报告成功。"""
        if self._memories is None:
            raise AgentError("当前应用未配置记忆管理。")
        return self._memories.undo_organization(
            memory_id,
            revision,
            global_scope=global_scope,
            session_id=self.session.session_id if self.session else None,
        )

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
        if self._store.has_unconfirmed_actions(self.session.session_id):
            return ContinuationStatus(
                False,
                "execution_unknown",
                "存在尚未完成或执行结果未知的操作；先核对文件与日志，再重启恢复或 /new；不会自动重跑。",
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

    def list_skills(self) -> tuple[SkillListing, ...]:
        """查询可用指导及覆盖来源；不读取正文、不创建会话、不请求模型。"""
        return self._skills.listings() if self._skills is not None else ()

    def send(
        self,
        text: str,
        *,
        on_output: Callable[[ModelOutput], None] | None = None,
        skill_id: str | None = None,
    ) -> str:
        """执行完整一轮并返回已保存回答；已知失败以 AgentError 交给界面。

        skill_id 由用户显式选择，正文只作用于本轮；读取失败不提交轮次或请求模型。
        已绑定模式时固定到本轮及子任务收尾结束，异常也会释放占用。
        中断后在所属线程回收宿主注册的交互资源，不重放先前动作；清理失败保留原中断。
        """
        with (
            self._interrupt_scope(),
            self._input_scope(),
            self._modes.turn() if self._modes is not None else nullcontext(),
        ):
            if skill_id is not None:
                self._conversation.clear_execution_report()
                if self._skills is None:
                    raise AgentError("当前应用未启用 Skill。")
                selection = self._skills.select(skill_id)
                return self._conversation.send(text, on_output=on_output, selected_skill=selection)
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

    def undo(self) -> UndoResult:
        """执行用户明确的单文件撤销；无会话不创建数据，过程固定模式且不请求模型。"""
        if self.session is None:
            raise AgentError("当前尚无会话，没有可撤销的文件编辑。")
        if self._undo is None:
            raise AgentError("当前应用没有配置撤销服务，请使用标准启动入口。")
        with (
            self._interrupt_scope(),
            self._modes.turn() if self._modes is not None else nullcontext(),
        ):
            return self._undo.undo(self.session.session_id)

    def compact(self) -> Summary | None:
        """生成并保存旧历史摘要，不修改原始记录。"""
        return self._conversation.compact()

    def preview_context(self, query: str = "") -> RequestContext:
        """查看基础上下文；可提供假设问题预览相关记忆，不提交消息或请求模型。"""
        return self._conversation.preview_context(query)

    def submit_input(self, text: str, event_id: str | None = None) -> EventReceipt:
        """向当前运行补充用户要求，不启动新轮次或访问会话JSONL。"""
        if self._running_inputs is None:
            raise AgentError("运行中输入未启用，请使用 --live-input。")
        return self._running_inputs.submit(text, event_id)

    def stop_current(self) -> None:
        """立即发出取消信号，不受补充队列容量限制。"""
        if self._running_inputs is None:
            raise AgentError("运行中输入未启用，请使用Ctrl+C。")
        self._running_inputs.stop()

    def check_interruption(self) -> None:
        """终端等待批准时检查当前运行停止信号，空闲终端不会被旧信号打断。"""
        if self._running_inputs is not None:
            self._running_inputs.check()

    @contextmanager
    def _interrupt_scope(self) -> Iterator[None]:
        """用户中断时回收当前会话附属任务；清理失败不覆盖原始中断。"""
        try:
            yield
        except (KeyboardInterrupt, EOFError, SystemExit) as error:
            if self._on_interrupt:
                try:
                    self._on_interrupt()
                except Exception:
                    error.add_note("附属后台任务或交互资源清理失败；原始中断保留，请检查任务记录与残留进程。")
            raise

    @contextmanager
    def _input_scope(self) -> Iterator[None]:
        """轮次退出时关闭补充入口；保存故障不覆盖原模型或工具异常。"""
        try:
            yield
        except BaseException as error:
            if self._running_inputs:
                try:
                    self._running_inputs.finish(False)
                except Exception:
                    error.add_note("补充输入收尾保存失败，记录可能仍为进行中。")
            raise
        else:
            if self._running_inputs:
                self._running_inputs.finish(True)

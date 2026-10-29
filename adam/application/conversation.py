"""组织会话流程；会话文件是已完成历史的来源。"""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import partial
from time import monotonic_ns
from typing import Any

from adam.application.request_policy import RequestPolicy
from adam.config.settings import AgentConfig, ContextConfig
from adam.context.builder import ContextBuilder, RequestContext
from adam.context.compaction import CompactionService
from adam.contracts.cancellation import CancellationToken
from adam.contracts.delegation import Delegation, RequestAccount
from adam.contracts.errors import (
    BudgetError,
    FailureCode,
    LLMError,
    RequestInterrupted,
    RequestNotSentError,
    StorageError,
    TurnSaveError,
)
from adam.contracts.history import Summary
from adam.contracts.memory import MemorySelection
from adam.contracts.models import Completion, ExecutionReport, ModelOutput, Session
from adam.contracts.planning import TaskPlanner
from adam.contracts.ports import ModelClient
from adam.session.outcome import ExecutionOutcome
from adam.session.storage import SessionStore
from adam.tool_calls.executor import ToolBudget, ToolExecutor


@dataclass
class TurnProgress:
    """本轮请求消费与自动压缩尝试；摘要同样占用模型请求次数。"""

    requests: RequestPolicy
    summary_attempted: bool = False
    pruning_announced: bool = False
    memory: MemorySelection = MemorySelection()


class Conversation:
    """管理当前会话并驱动一轮处理；依赖与默认模型由组装入口注入。"""

    def __init__(
        self,
        store: SessionStore,
        client: ModelClient,
        system_prompt: str,
        tools: ToolExecutor,
        limits: AgentConfig,
        *,
        model: str,
        context_config: ContextConfig | None = None,
        notify: Callable[[str], None] | None = None,
        accounting: Callable[[str], RequestAccount] | None = None,
        delegation: Delegation | None = None,
        planning: TaskPlanner | None = None,
        cancel: CancellationToken | None = None,
        on_turn_started: Callable[[str], None] | None = None,
        memory_provider: Callable[[str, int], MemorySelection] | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.system_prompt = system_prompt
        self.session: Session | None = None
        self.model = model
        self.limits = limits
        self.tool_executor = tools
        self._last_turn: tuple[str, str] | None = None
        self.context_builder = ContextBuilder(
            context_config or ContextConfig(), self.limits.max_output_tokens
        )
        self.notify = notify or (lambda _: None)
        self.accounting, self.delegation = accounting, delegation
        self.cancel, self.on_turn_started = cancel, on_turn_started
        self.memory_provider = memory_provider
        self.planning = planning

    def new(self, title: str = "新对话") -> Session:
        """创建并选中新会话，返回会话元信息。"""
        session_id = self.store.create_session(title, self.system_prompt)
        return self.resume(session_id)

    def resume(self, session_id: str) -> Session:
        """验证历史并切换会话；失败时保留原会话。"""
        session = self.store.select_session(session_id)
        self.store.load_messages(session_id)
        self.session = session
        self._last_turn = None
        return session

    def send(self, text: str, *, on_output: Callable[[ModelOutput], None] | None = None) -> str:
        """处理用户输入，完成模型与工具循环，返回已保存的最终回答。

        输入、请求和工具轨迹独立留痕；失败轮次不加入有效历史。
        已知失败遵循 AgentError 契约：预算问题为 BudgetError，模型问题为 LLMError；
        回答生成后保存失败为携带 partial_reply 的 TurnSaveError。
        请求与轮次分别收尾，保存稳定原因码和单调时钟耗时；原始异常继续传播。
        轮次耗时从输入登记完成算到终态生成，包含工具及中间落盘，不含终态自身写入。
        工具诊断写入失败或未知副作用为 StorageError；命令/编辑日志失败
        为 CommandLogError / EditLogError；用户拒绝为 ApprovalDeniedError，均停止本轮。
        意外的程序错误继续向上传播。
        """
        self._last_turn = None
        session = self.session or self.new(text[:40])
        turn_id = self.store.start_turn(session.session_id, text)
        self._last_turn = (session.session_id, turn_id)
        started_ns = monotonic_ns()
        try:
            if self.planning:
                self.planning.begin(session.session_id, turn_id)
            if self.delegation:
                self.delegation.begin(session.session_id, turn_id, self.model)
            if self.on_turn_started:
                self.on_turn_started(turn_id)
            reply = self._run_turn(session, turn_id, text, started_ns, on_output)
        except BaseException as error:
            if self.delegation:
                self._save_failure(self.delegation.end, error)
            outcome = ExecutionOutcome.failure(error, self._elapsed_ms(started_ns))
            self._save_failure(
                lambda: self.store.finish_turn(session.session_id, turn_id, outcome),
                error,
            )
            raise
        if self.delegation:
            self.delegation.end()
        return reply

    def usage(self) -> tuple[int, bool]:
        """返回当前主会话及已接入子会话的消费；无会话时不创建文件。"""
        if self.session is None:
            return 0, False
        if self.accounting:
            return self.accounting(self.session.session_id).usage()
        return self.store.usage(self.session.session_id)

    def execution_report(self) -> ExecutionReport | None:
        """读取本次运行最近一次输入的事实；未成功登记输入或切换会话后返回 None。

        已失败的轮次也可查询；不重新执行工具、不加入模型历史，读取失败抛 StorageError。
        """
        if self._last_turn is None:
            return None
        return self.store.execution_report(*self._last_turn)

    def compact(self) -> Summary | None:
        """手动压缩当前会话；无会话或无旧历史不请求模型，不创建空会话。"""
        if self.session is None:
            self.notify("没有当前会话，无需摘要。")
            return None
        self._check_execution_state(self.session.session_id)
        summary = self._compact(self.session)
        if summary is None:
            self.notify("没有可摘要的旧轮次。")
        else:
            self.notify(
                f"上下文> 已保存摘要，累计覆盖 {len(summary.covered_turn_ids)} 轮；原始日志保留。"
            )
        return summary

    def preview_context(self, query: str = "") -> RequestContext:
        """预览当前会话的下一次基础输入；不创建会话、请求模型或修改日志。

        包含系统提示、成功历史和当前工具说明；query 为空时不含下一条新问题。
        显式提供 query 时仅用于假设输入和记忆选择，不提交为聊天消息；
        未选会话时使用本次配置的系统提示。超预算仍可查看，不冒充最近实际请求。
        """
        turn = [{"role": "user", "content": query}] if query else []
        memory = self._select_memory(self.session, turn, query)
        return replace(self._build_context(self.session, turn, memory), preview_query=query)

    def _run_turn(
        self,
        session: Session,
        turn_id: str,
        text: str,
        started_ns: int,
        on_output: Callable[[ModelOutput], None] | None,
    ) -> str:
        """运行已登记轮次；started_ns 为本进程计时起点，完整消息提交成功后才返回。"""
        self._check_execution_state(session.session_id)
        turn: list[dict[str, Any]] = [{"role": "user", "content": text}]
        seen_call_ids: set[str] = set()

        progress = TurnProgress(
            RequestPolicy(self.limits.max_model_requests, self.notify, cancel=self.cancel),
            memory=self._select_memory(session, turn, text),
        )
        if progress.memory.warning:
            self.notify(progress.memory.warning)
        if self.delegation:
            self.delegation.set_memory(progress.memory)
        tool_budget = ToolBudget(self.limits.max_edit_attempts)
        while progress.requests.used < self.limits.max_model_requests:
            if self.cancel:
                self.cancel.check()
            context = self._prepare_context(session, turn_id, turn, progress)
            # 未收集子结果或计划未闭合时暂存正文，避免提前流式展示即将被要求收尾的回答。
            ready = (not self.delegation or self.delegation.accept_final()) and (
                not self.planning or not self.planning.unfinished()
            )
            output = on_output if ready else None
            request_id, result = progress.requests.run(
                partial(self._request_with_usage, session.session_id, turn_id, context, output)
            )
            calls = result.message.get("tool_calls") or []
            if not calls and self.delegation and not self.delegation.accept_final():
                self.notify("子 Agent 结果尚未收集，要求主 Agent 先等待并汇总。")
                continue
            if (
                not calls
                and self.planning
                and self.planning.retry_final(
                    progress.requests.used < self.limits.max_model_requests
                )
            ):
                continue
            turn.append(result.message)
            if not calls:
                return self._save_completed_turn(session.session_id, turn_id, turn, started_ns)

            self._check_tool_budget(
                session.session_id, progress.requests.used - 1, calls, seen_call_ids
            )
            turn.extend(
                self.tool_executor.execute(
                    session.session_id, turn_id, request_id, calls, tool_budget
                )
            )
            seen_call_ids.update(call["id"] for call in calls)

        raise BudgetError("达到本轮请求上限。", code=FailureCode.REQUEST_LIMIT)

    def _check_execution_state(self, session_id: str) -> None:
        """命令或文件修改结果未知时禁止继续调用模型；只读诊断仍可使用。"""
        operations = self.store.load_executions(session_id) + self.store.load_edits(session_id)
        if any(item["status"] == "unknown" for item in operations):
            raise StorageError(
                "会话有结果未知的命令或文件修改；请检查记录并新建会话，不自动重做。",
                code=FailureCode.EXECUTION_UNKNOWN,
            )

    def _prepare_context(
        self, session: Session, turn_id: str, turn: list[dict[str, Any]], progress: TurnProgress
    ) -> RequestContext:
        """先缩旧工具，再按压力摘要，最后使用整轮选择；预览不经过此有副作用入口。"""
        self._check_session_budget(session.session_id)
        context = self._build_context(session, turn, progress.memory)
        # 受保护部分本身放不下时，先停止，避免花费一次无法解决问题的摘要请求。
        context.ensure_within_budget()
        limits = self.context_builder.limits
        pressure = context.candidate_input_tokens + limits.safety_margin_tokens
        if (
            limits.auto_compact
            and not progress.summary_attempted
            and pressure >= limits.max_input_tokens * limits.compression_trigger_ratio
        ):
            progress.summary_attempted = True
            service = CompactionService(
                self.store, self.client, limits, self._check_session_budget, self.accounting
            )
            desired = max(
                1,
                int(
                    pressure
                    - limits.max_input_tokens * limits.compression_target_ratio
                    + limits.summary_max_output_tokens
                    - context.estimate.summary_tokens
                ),
            )
            plan = service.plan(session, prune_tools=True, min_source_tokens=desired)
            if plan is not None:
                # 为摘要后的正式请求至少留一次额度；不花掉最后一次后才发现无法回答。
                if progress.requests.used + 2 > self.limits.max_model_requests:
                    raise BudgetError(
                        "剩余请求次数不足以摘要并继续回答。", code=FailureCode.REQUEST_LIMIT
                    )
                progress.requests.run(
                    lambda: service.execute(session, self.model, plan, turn_id=turn_id),
                    keep_requests=1,
                )
                context = self._build_context(session, turn, progress.memory)
                self.notify("上下文> 已自动更新历史摘要；原始日志保留。")
                progress.pruning_announced = True
        if context.pruned_tools and not progress.pruning_announced:
            self.notify("上下文> 已缩减旧工具结果；原始日志保留。")
            progress.pruning_announced = True
        return context

    def _compact(self, session: Session, turn_id: str | None = None) -> Summary | None:
        """委托独立摘要服务，复用累计消费检查；不把摘要加入普通问答历史。"""
        service = CompactionService(
            self.store,
            self.client,
            self.context_builder.limits,
            self._check_session_budget,
            self.accounting,
        )
        plan = service.plan(session)
        if plan is None:
            return None
        policy = RequestPolicy(self.limits.max_model_requests, self.notify, cancel=self.cancel)
        return policy.run(lambda: service.execute(session, self.model, plan, turn_id=turn_id))

    def _select_memory(
        self, session: Session | None, turn: list[dict[str, Any]], query: str
    ) -> MemorySelection:
        """先预留受保护输入，再选一次记忆；工具循环复用此快照，不重新读取库。"""
        if self.memory_provider is None:
            return MemorySelection()
        capacity = self._build_context(session, turn).memory_capacity
        return self.memory_provider(query, capacity)

    def _build_context(
        self,
        session: Session | None,
        turn: list[dict[str, Any]],
        memory: MemorySelection | None = None,
    ) -> RequestContext:
        """读取成功历史并委托构造；预览与每次真实请求共用，工具结果加入后重新估算。"""
        history = self.store.load_turns(session.session_id) if session is not None else []
        return self.context_builder.build(
            model=self.model,
            system_prompt=(session.system_prompt if session is not None else self.system_prompt)
            + (
                self.delegation.instructions(session.session_id if session else None)
                if self.delegation
                else ""
            )
            + (
                self.planning.instructions(session.session_id if session else None)
                if self.planning
                else ""
            ),
            history=history,
            current_turn=turn,
            tools=self.tool_executor.definitions(),
            summary=self.store.load_summary(session.session_id) if session is not None else None,
            memory=memory,
        )

    def _check_session_budget(self, session_id: str) -> None:
        """用量未知或累计额度耗尽时停止，避免静默漏记后继续请求。"""
        if self.accounting:
            self.accounting(session_id).check()
            return
        tokens, unknown = self.store.usage(session_id)
        if unknown:
            raise BudgetError(
                "会话预算统计不完整（用量未知或请求中断）；请新建会话，不自动重发。",
                code=FailureCode.USAGE_UNKNOWN,
            )
        if tokens >= self.limits.max_session_tokens:
            raise BudgetError(
                "会话 token 额度已用完，请提高配置额度或新建会话。",
                code=FailureCode.SESSION_TOKEN_LIMIT,
            )

    def _request_with_usage(
        self,
        session_id: str,
        turn_id: str,
        context: RequestContext,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> tuple[str, Completion]:
        """执行一次物理请求：检查预算、登记、调用和结算；外层策略决定是否重试。

        返回本次 request_id 与包含可靠用量的 Completion，供工具关联来源请求。
        turn_id 指向已记录的用户输入；context 是本次构造结果，先检查输入预算再登记请求。
        本地超限不伪造请求或未知用量；有效响应先记请求成功，缺少用量时整轮仍停止。
        明确未发出的连接错误结算为零；只有结算成功才允许外层重试。
        中断或未知用量会阻止继续消费。最终回答记账失败时保留回答供终端显示；
        请求耗时只包含完整模型调用及响应解析，不含开始与结算日志写入。
        失败收尾不遮蔽原始异常；崩溃遗留状态由下次取得会话写锁时恢复。
        """
        self._check_session_budget(session_id)
        context.ensure_within_budget()
        scope = (
            self.accounting(session_id).request(
                context.budgeted_input_tokens + context.max_output_tokens
            )
            if self.accounting
            else nullcontext()
        )
        with scope:
            return self._perform_request(session_id, turn_id, context, on_output)

    def _perform_request(
        self,
        session_id: str,
        turn_id: str,
        context: RequestContext,
        on_output: Callable[[ModelOutput], None] | None,
    ) -> tuple[str, Completion]:
        """在额度预留内登记、调用与结算；共享额度退出时读取本次持久化结果。"""
        if self.cancel:
            self.cancel.check()
        request_id = self.store.start_request(
            session_id, turn_id, context.model, context=context.selection_record()
        )
        started_ns = monotonic_ns()
        try:
            result = self.client.complete(
                context.messages,
                model=context.model,
                tools=context.tools,
                max_tokens=context.max_output_tokens,
                on_output=on_output,
            )
        except BaseException as error:
            usage = error.usage if isinstance(error, (LLMError, RequestInterrupted)) else None
            outcome = ExecutionOutcome.failure(error, self._elapsed_ms(started_ns))
            if isinstance(error, RequestNotSentError):
                # 未发出请求允许重试的前提是失败及零用量已可靠记录；日志失败直接停止。
                self.store.finish_request(session_id, turn_id, request_id, usage, outcome=outcome)
            else:
                self._save_failure(
                    lambda: self.store.finish_request(
                        session_id, turn_id, request_id, usage, outcome=outcome
                    ),
                    error,
                )
            raise

        outcome = ExecutionOutcome.success(self._elapsed_ms(started_ns))
        try:
            self.store.finish_request(
                session_id, turn_id, request_id, result.usage, outcome=outcome
            )
        except StorageError as error:
            if not result.message.get("tool_calls"):
                raise TurnSaveError(
                    result.message["content"], code=FailureCode.REQUEST_SAVE_FAILED
                ) from error
            raise StorageError(
                "请求结算记录保存失败；已停止本轮，未自动重试。",
                code=FailureCode.REQUEST_SAVE_FAILED,
            ) from error
        if result.usage is None:
            raise LLMError(
                "API未返回可靠用量；预算统计不完整，本轮停止。", code=FailureCode.USAGE_UNKNOWN
            )
        return request_id, result

    def _check_tool_budget(
        self, session_id: str, step: int, calls: list[dict[str, Any]], seen_ids: set[str]
    ) -> None:
        """执行整批工具前检查额度及重复 ID；step 为从零开始的请求序号，包含自动摘要。"""
        # 没有下一次模型请求额度时，不执行无法用于生成最终回答的工具。
        if step + 1 >= self.limits.max_model_requests:
            raise BudgetError(
                "达到本轮模型请求次数上限，工具链未加入有效历史。", code=FailureCode.REQUEST_LIMIT
            )
        if len(seen_ids) + len(calls) > self.limits.max_tool_calls:
            raise BudgetError(
                "达到本轮工具调用次数上限，工具链未加入有效历史。", code=FailureCode.TOOL_LIMIT
            )
        self._check_session_budget(session_id)
        if any(call["id"] in seen_ids for call in calls):
            raise LLMError("模型重复使用工具调用ID，本轮停止。", code=FailureCode.INVALID_RESPONSE)

    def _save_completed_turn(
        self, session_id: str, turn_id: str, turn: list[dict[str, Any]], started_ns: int
    ) -> str:
        """成功原因、截至提交前的耗时与完整消息同块保存；保存失败时携带原回答。"""
        reply = turn[-1]["content"]
        try:
            self.store.append_messages(
                session_id,
                turn_id,
                turn,
                outcome=ExecutionOutcome.success(self._elapsed_ms(started_ns)),
            )
        except StorageError as error:
            raise TurnSaveError(reply) from error
        return reply

    @staticmethod
    def _elapsed_ms(started_ns: int) -> int:
        """单调时钟差转为整毫秒；跨进程不能复用起点，不用系统时间相减。"""
        return (monotonic_ns() - started_ns) // 1_000_000

    @staticmethod
    def _save_failure(save: Callable[[], None], original: BaseException) -> None:
        """尽力保存失败状态；日志故障不能覆盖最初的模型、工具或程序异常。"""
        try:
            save()
        except Exception:
            # 磁盘持续不可写时无法再可靠记录终态；附注不带原始系统或服务端详情。
            original.add_note("执行状态保存失败，日志可能仍显示进行中；未自动重试。")

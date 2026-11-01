"""执行模型请求的工具并独立记录轨迹，后续模型失败也保留执行证据。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import get_ident
from typing import Any

from adam.config.settings import NoProgressConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ApprovalDeniedError, NoProgressError, ToolError
from adam.contracts.execution import CODE_TOOL_NAME, ToolBudget
from adam.contracts.hooks import HookService
from adam.contracts.runtime import FailureState
from adam.session.storage import SessionStore
from adam.tool_calls.artifact_tools import ToolArtifacts
from adam.tool_calls.discovery_tools import ToolDiscovery
from adam.tool_calls.file_evidence import FileResult
from adam.tool_calls.invocation import ToolInvocation
from adam.tool_calls.progress import NoProgressGuard, ProgressAction
from adam.tool_calls.registry import PreparedAction, ToolRegistry
from adam.tool_calls.scheduler import ToolResult, ToolScheduler, ToolTask


@dataclass(frozen=True)
class _NestedScope:
    """只在串行编排动作存活期间可用；名单和额度来自触发父调用的同一次请求。"""

    parent: ToolInvocation
    budget: ToolBudget
    provided: frozenset[str] | None
    cancel: CancellationToken
    thread_id: int


class ToolExecutor:
    """协调日志与工具调用；注册表分派动作，具体工具执行权限与确认规则。"""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry,
        notify: Callable[[str], None],
        *,
        before_call: Callable[[dict[str, str]], None] | None = None,
        after_call: Callable[[], None] | None = None,
        check_call: Callable[[str], None] | None = None,
        cancel: CancellationToken | None = None,
        hooks: HookService | None = None,
        artifacts: ToolArtifacts | None = None,
        on_notice: Callable[[str], None] | None = None,
        discovery: ToolDiscovery | None = None,
        no_progress: NoProgressConfig | None = None,
    ) -> None:
        """注入存储和工具目录；notify 只接收工具名称，展示方式由调用方决定。

        after_call 在结果落盘后发布业务变化，界面异常向上传播。
        check_call 在副作用之前检查协作限制；ToolError 留下失败结果供模型调整。
        hooks 只接检查接口，on_notice 报告后置检查故障；未配置事件不产生检查记录。
        no_progress 默认启用；每个执行器按会话/轮次重建独立检测状态，不跨子任务共享。
        """
        self.store = store
        self.registry = registry
        self.notify = notify
        self.before_call, self.cancel = before_call, cancel
        self.after_call, self.check_call = after_call, check_call
        self.hooks, self.on_notice = hooks, on_notice or (lambda _: None)
        self.artifacts, self.discovery = artifacts, discovery
        self._progress_config = no_progress or NoProgressConfig()
        self._progress_scope: tuple[str, str] | None = None
        self._progress = NoProgressGuard(self._progress_config)
        # Git 取消沿用进程树清理（最多等待 taskkill 5 秒），为直接进程回收留出余量。
        self._scheduler = ToolScheduler(cancel=cancel, cleanup_timeout_seconds=7.0)
        self._nested_scope: _NestedScope | None = None
        self._inside_nested = False

    def definitions(self, *, turn_id: str | None = None) -> list[dict[str, Any]]:
        """取得本轮选择或无副作用初始预览；未接发现策略的显式执行器保持全量目录。"""
        if self.discovery:
            return self.discovery.definitions(turn_id)
        return self.registry.definitions()

    def failure_state(self, session_id: str, turn_id: str) -> FailureState:
        """只读当前轮次失败观测；禁用为未知，新轮次为零，不重置旧轮次检测器。"""
        if not self._progress_config.enabled:
            return FailureState()
        if self._progress_scope != (session_id, turn_id):
            return FailureState(0)
        decision = self._progress.decision
        return FailureState(decision.consecutive_failures, decision.fingerprint)

    def execute(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
        budget: ToolBudget,
        *,
        provided_names: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """执行 calls，安全读取最多两项并发，其他动作串行；返回原 ID 顺序的 tool 消息。

        turn_id 和 request_id 指向产生这些工具调用的轮次与模型请求。
        budget 在同轮各批及嵌套调用间共用，超限时保留先前已执行结果并停止。
        provided_names 固定触发此批调用的实际请求名单，同批发现不会扩展它；
        只有未接发现服务的显式执行器可以省略，聊天入口始终传入。
        预检、Hook、来源绑定和所有日志由本线程完成；工作线程只执行已注册的读取动作。
        开始记录写入后才执行，结果落盘后才返回；存储错误停止本轮，拒绝停止剩余动作。
        重复失败按原调用顺序观察；提醒写入工具结果，达到上限先落结果再停止派发。
        """
        if self.discovery is not None and provided_names is None:
            raise ValueError("发现执行器缺少本次请求的工具名单。")
        self._enter_turn(session_id, turn_id)
        provided = frozenset(provided_names) if provided_names is not None else None

        invocations = [
            ToolInvocation(
                self.store,
                self.registry,
                session_id,
                {"turn_id": turn_id, "request_id": request_id, "tool_call_id": call["id"]},
                call["function"],
                self.notify,
                hooks=self.hooks,
                artifacts=self.artifacts,
                on_notice=self.on_notice,
                check_call=partial(self._check_provided, provided=provided),
            )
            for call in calls
        ]
        by_id = {item.call_id: item for item in invocations}
        results: list[dict[str, Any]] = []

        def finish(result: ToolResult[dict[str, Any]]) -> None:
            output = self._finish(by_id[result.call_id], result.value)
            results.append({"role": "tool", "tool_call_id": result.call_id, "content": output})

        tasks = [
            ToolTask(
                item.call_id,
                partial(self._prepare, item, budget, provided),
                self.registry.capability(item.name),
                self.registry.can_parallelize(item.name),
            )
            for item in invocations
        ]
        self._scheduler.run(tasks, on_result=finish)
        return results

    def execute_nested(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """为当前 execute_code 派发一个底层工具，返回原有界结果，不加入模型消息。

        只能由父动作所在协调线程调用；ID 由宿主分配，不能自选请求或扩大工具名单。
        复用原预检、Hook、批准、附件与日志。取消、拒绝和额度/保存故障向上传播，
        父 worker 必须停止；这里不重入批次调度器，也不重试失败的动作。
        """
        scope = self._nested_scope
        if scope is None or scope.thread_id != get_ident() or self._inside_nested:
            raise RuntimeError("嵌套工具只能在本线程的编排动作中调用，不能再次嵌套。")
        scope.cancel.check()
        parent = scope.parent
        self._enter_turn(parent.session_id, parent.source["turn_id"])
        invocation = ToolInvocation(
            self.store,
            self.registry,
            parent.session_id,
            {**parent.source, "tool_call_id": self.store.new_identity("nested_tool", 16)},
            {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False)},
            self.notify,
            hooks=self.hooks,
            artifacts=self.artifacts,
            on_notice=self.on_notice,
            check_call=partial(self._check_provided, provided=scope.provided, nested=True),
            parent_call_id=parent.call_id,
        )
        self._inside_nested = True
        try:
            action = self._prepare(invocation, scope.budget, scope.provided)
            scope.cancel.check()
            output = self._finish(invocation, action(scope.cancel))
            return json.loads(output)
        finally:
            self._inside_nested = False
            # 主计划、反思与协作服务共享当前来源；后续父动作不能沿用最后一个子调用 ID。
            if self.before_call:
                self.before_call(parent.source)

    def _check_provided(
        self, name: str, *, provided: frozenset[str] | None, nested: bool = False
    ) -> None:
        """嵌套也固定使用父请求名单；发现只能影响后续模型请求，不开放递归编排。"""
        if nested and name == CODE_TOOL_NAME:
            raise ToolError("编排不能递归调用 execute_code。")
        if self.discovery is not None and provided is not None:
            self.discovery.require_provided(name, provided)
        if provided is not None and name not in provided:
            raise ToolError("本次请求尚未提供该工具的完整说明；请先发现，再在后续请求调用。")
        if self.check_call:
            self.check_call(name)

    def _enter_turn(self, session_id: str, turn_id: str) -> None:
        """同轮跨请求累计；显式调用已停止轮次也不能绕过限制执行下一批。"""
        scope = (session_id, turn_id)
        if self._progress_scope != scope:
            self._progress_scope = scope
            self._progress = NoProgressGuard(self._progress_config)
        if self._progress.decision.action is ProgressAction.STOP:
            raise NoProgressError(self._progress.decision.consecutive_failures)

    def _finish(self, invocation: ToolInvocation, result: dict[str, Any]) -> str:
        """观察实际结果，再持久化模型可见反馈；日志/回调故障保留原错误，不伪装成熔断。"""
        decision = self._progress.observe(
            invocation.name,
            invocation.arguments,
            result,
            evidence_version=result.version if isinstance(result, FileResult) else None,
        )
        output = invocation.finish(result, progress=decision)
        if self.after_call:
            self.after_call()
        if result.get("status") == "denied":
            raise ApprovalDeniedError("已拒绝本次动作，本轮已停止；此前完成的动作保留。")
        if decision.action is ProgressAction.STOP:
            raise NoProgressError(decision.consecutive_failures)
        return output

    def _prepare(
        self, invocation: ToolInvocation, budget: ToolBudget, provided: frozenset[str] | None
    ) -> PreparedAction:
        """在协调线程占用额度并绑定本次来源；串行动作因此不会使用别的调用来源。"""
        budget.consume(invocation.name)
        if self.before_call:
            self.before_call(invocation.source)
        action = invocation.prepare()
        if invocation.name == CODE_TOOL_NAME and invocation.parent_call_id is None:
            if self.registry.can_parallelize(invocation.name):
                raise RuntimeError("编排入口必须在协调线程串行执行。")
            return partial(self._run_parent, invocation, budget, provided, action)
        return action

    def _run_parent(
        self,
        invocation: ToolInvocation,
        budget: ToolBudget,
        provided: frozenset[str] | None,
        action: PreparedAction,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """限定嵌套能力的存活时间；父动作结束或失败后，旧回调无法继续派发。"""
        if self._nested_scope is not None:
            raise RuntimeError("编排动作不可重入。")
        self._nested_scope = _NestedScope(invocation, budget, provided, cancel, get_ident())
        try:
            return action(cancel)
        finally:
            self._nested_scope = None

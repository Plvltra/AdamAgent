"""执行模型请求的工具并独立记录轨迹，后续模型失败也保留执行证据。"""

import json
from collections.abc import Callable
from functools import partial
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ApprovalDeniedError
from adam.contracts.execution import ToolBudget
from adam.contracts.hooks import HookService
from adam.session.storage import SessionStore
from adam.tool_calls.artifact_tools import ToolArtifacts
from adam.tool_calls.invocation import ToolInvocation
from adam.tool_calls.registry import PreparedAction, ToolRegistry
from adam.tool_calls.scheduler import ToolResult, ToolScheduler, ToolTask


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
    ) -> None:
        """注入存储和工具目录；notify 只接收工具名称，展示方式由调用方决定。

        after_call 在结果落盘后发布业务变化，界面异常向上传播。
        check_call 在副作用之前检查协作限制；ToolError 留下失败结果供模型调整。
        hooks 只接检查接口，on_notice 报告后置检查故障；未配置事件不产生检查记录。
        """
        self.store = store
        self.registry = registry
        self.notify = notify
        self.before_call, self.cancel = before_call, cancel
        self.after_call, self.check_call = after_call, check_call
        self.hooks, self.on_notice = hooks, on_notice or (lambda _: None)
        self.artifacts = artifacts
        # Git 取消沿用进程树清理（最多等待 taskkill 5 秒），为直接进程回收留出余量。
        self._scheduler = ToolScheduler(cancel=cancel, cleanup_timeout_seconds=7.0)

    def definitions(self) -> list[dict[str, Any]]:
        """返回此执行器实际支持的工具说明，供模型提出调用。"""
        return self.registry.definitions()

    def execute(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
        budget: ToolBudget,
    ) -> list[dict[str, Any]]:
        """执行 calls，安全读取最多两项并发，其他动作串行；返回原 ID 顺序的 tool 消息。

        turn_id 和 request_id 指向产生这些工具调用的轮次与模型请求。
        budget 在同轮各批工具间共用，编辑超限时保留先前已执行结果并停止。
        预检、Hook、来源绑定和所有日志由本线程完成；工作线程只执行已注册的读取动作。
        开始记录写入后才执行，结果落盘后才返回；存储错误停止本轮，拒绝停止剩余动作。
        """
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
                check_call=self.check_call,
            )
            for call in calls
        ]
        by_id = {item.call_id: item for item in invocations}
        results: list[dict[str, Any]] = []

        def finish(result: ToolResult[dict[str, Any]]) -> None:
            output = by_id[result.call_id].finish(result.value)
            results.append({"role": "tool", "tool_call_id": result.call_id, "content": output})
            if self.after_call:
                self.after_call()
            if json.loads(output).get("status") == "denied":
                raise ApprovalDeniedError("已拒绝本次动作，本轮已停止；此前完成的动作保留。")

        tasks = [
            ToolTask(
                item.call_id,
                partial(self._prepare, item, budget),
                self.registry.capability(item.name),
                self.registry.can_parallelize(item.name),
            )
            for item in invocations
        ]
        self._scheduler.run(tasks, on_result=finish)
        return results

    def _prepare(self, invocation: ToolInvocation, budget: ToolBudget) -> PreparedAction:
        """在协调线程占用额度并绑定本次来源；串行动作因此不会使用别的调用来源。"""
        budget.consume(invocation.name)
        if self.before_call:
            self.before_call(invocation.source)
        return invocation.prepare()

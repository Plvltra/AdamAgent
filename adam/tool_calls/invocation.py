"""单个工具调用的预检、动作日志和后置检查；全部存储操作留在协调线程。"""

import secrets
from collections.abc import Callable
from typing import Any

from adam.contracts.errors import CommandLogError, EditLogError, StorageError, ToolError
from adam.contracts.hooks import HookService
from adam.session.storage import SessionStore
from adam.tool_calls.lifecycle import ToolHooks
from adam.tool_calls.registry import PreparedAction, ToolRegistry


class ToolInvocation:
    """固定一个调用的来源和 Hook 快照，避免并发读取共享当前调用状态。"""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry,
        session_id: str,
        source: dict[str, str],
        function: dict[str, Any],
        notify: Callable[[str], None],
        *,
        hooks: HookService | None = None,
        on_notice: Callable[[str], None],
        check_call: Callable[[str], None] | None = None,
    ) -> None:
        """绑定已校验调用；source 含 turn_id、request_id、tool_call_id，function 含名称与原始参数。"""
        self.store, self.registry, self.session_id = store, registry, session_id
        self.source = dict(source)
        self.name, self.arguments = function["name"], function["arguments"]
        self.call_id = source["tool_call_id"]
        self.notify, self.check_call = notify, check_call
        self._execution_id = secrets.token_hex(16)
        self._hooks = (
            ToolHooks(hooks, store, session_id, source, on_notice) if hooks is not None else None
        )

    def prepare(self) -> PreparedAction:
        """先保存开始事件，再校验与运行前置 Hook；返回动作，不在此执行读取。

        通用开始事件表示进入工具处理；命令和编辑的批准/副作用边界分别由 command_* /
        edit_* 记录。日志故障停止本轮，不能变成可重试工具错误。
        """
        self.store.append_tool_event(
            self.session_id,
            "tool_started",
            {**self.source, "name": self.name, "arguments": self.arguments},
        )
        self.notify(self.name)
        try:
            if self.check_call:
                self.check_call(self.name)
            return self.registry.prepare(
                self.name,
                self.arguments,
                record_execution=self._record_action,
                before_tool=self._hooks.before if self._hooks else None,
            )
        except ToolError as error:
            output = {"ok": False, "error": str(error)}
            return lambda _cancel: output

    def finish(self, result: dict[str, Any]) -> str:
        """编码并可靠保存实际结果后运行后置 Hook；后置故障不能改写已发生的动作。"""
        output = self.registry.encode(self.name, result)
        self.store.append_tool_event(
            self.session_id, "tool_finished", {**self.source, "output": output}
        )
        if self._hooks:
            self._hooks.after(output)
        return output

    def _record_action(self, kind: str, payload: dict[str, Any]) -> None:
        """串行动作按事件类型保存边界；只读并发入口不会接收此回调。"""
        if kind.startswith("edit_"):
            try:
                self.store.append_edit_event(
                    self.session_id,
                    kind,
                    {**payload, **self.source, "edit_id": self._execution_id},
                )
            except (StorageError, ValueError) as error:
                raise EditLogError(
                    "编辑日志保存失败；实际结果可能未知，已停止且不自动重做。"
                ) from error
        else:
            try:
                self.store.append_command_event(
                    self.session_id,
                    kind,
                    {**payload, **self.source, "execution_id": self._execution_id},
                )
            except StorageError as error:
                raise CommandLogError(
                    "命令执行日志保存失败；执行状态可能未知，未自动重试。"
                ) from error
            except ValueError as error:
                # 日志关联错误不能被注册表当成模型参数错误后继续执行。
                raise CommandLogError("命令执行日志校验失败；已停止本轮，未自动重试。") from error

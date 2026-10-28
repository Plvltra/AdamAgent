"""执行模型请求的工具并独立记录轨迹，后续模型失败也保留执行证据。"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from adam.errors import (
    ApprovalDeniedError,
    BudgetError,
    CommandLogError,
    EditLogError,
    FailureCode,
    StorageError,
)
from adam.storage import SessionStore
from adam.tool_calls.registry import ToolRegistry


@dataclass
class ToolBudget:
    """单轮专用编辑额度；每次派发前占用，参数错误或写入失败也不退还。"""

    max_edit_attempts: int
    edit_attempts: int = 0

    def consume(self, name: str) -> None:
        """超限时在开始记录、确认与文件操作之前停止；普通工具不消耗编辑额度。"""
        if name != "replace_text":
            return
        if self.edit_attempts >= self.max_edit_attempts:
            raise BudgetError("达到本轮编辑尝试次数上限。", code=FailureCode.EDIT_LIMIT)
        self.edit_attempts += 1


class ToolExecutor:
    """协调日志与工具调用；注册表分派动作，具体工具执行权限与确认规则。"""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry,
        notify: Callable[[str], None],
    ) -> None:
        """注入存储和工具目录；notify 只接收工具名称，展示方式由调用方决定。"""
        self.store = store
        self.registry = registry
        self.notify = notify

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
        """串行执行 calls，返回与调用 ID 配对的 tool 消息。

        turn_id 和 request_id 指向产生这些工具调用的轮次与模型请求。
        budget 在同轮各批工具间共用，编辑超限时保留先前已执行结果并停止。
        开始记录写入后才执行，结果落盘后才返回；存储错误通过 AgentError 契约向上抛，
        禁止模型在执行轨迹无法保存时继续行动；拒绝结果保存后立即停止本批和本轮。
        """
        results = []
        for call in calls:
            budget.consume(call["function"]["name"])
            link = {
                "turn_id": turn_id,
                "request_id": request_id,
                "tool_call_id": call["id"],
            }
            output = self._execute_call(session_id, link, call["function"])
            results.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            if json.loads(output).get("status") == "denied":
                raise ApprovalDeniedError("已拒绝本次动作，本轮已停止；此前完成的动作保留。")
        return results

    def _execute_call(self, session_id: str, link: dict[str, str], function: dict[str, Any]) -> str:
        """保留参数和回传结果；命令及编辑独立记录批准与副作用边界。"""
        self.store.append_tool_event(
            session_id,
            "tool_started",
            {**link, "name": function["name"], "arguments": function["arguments"]},
        )
        execution_id = secrets.token_hex(16)

        def record_command(kind: str, payload: dict[str, Any]) -> None:
            try:
                self.store.append_command_event(
                    session_id, kind, {**payload, **link, "execution_id": execution_id}
                )
            except StorageError as error:
                raise CommandLogError(
                    "命令执行日志保存失败；执行状态可能未知，未自动重试。"
                ) from error
            except ValueError as error:
                # 日志关联错误不能被注册表当成模型参数错误后继续执行。
                raise CommandLogError("命令执行日志校验失败；已停止本轮，未自动重试。") from error

        def record_edit(kind: str, payload: dict[str, Any]) -> None:
            try:
                self.store.append_edit_event(
                    session_id, kind, {**payload, **link, "edit_id": execution_id}
                )
            except (StorageError, ValueError) as error:
                raise EditLogError(
                    "编辑日志保存失败；实际结果可能未知，已停止且不自动重做。"
                ) from error

        def record_action(kind: str, payload: dict[str, Any]) -> None:
            """按日志事件类型保存边界；注册表无需识别具体工具或存储实现。"""
            if kind.startswith("edit_"):
                record_edit(kind, payload)
            else:
                record_command(kind, payload)

        self.notify(function["name"])
        # 通用开始事件表示进入工具处理；命令和编辑的批准/副作用边界分别由 command_* / edit_* 记录。
        output = self.registry.execute(
            function["name"],
            function["arguments"],
            record_execution=record_action,
        )
        self.store.append_tool_event(session_id, "tool_finished", {**link, "output": output})
        return output

"""执行模型请求的工具并独立记录轨迹，后续模型失败也保留执行证据。"""

import secrets
from collections.abc import Callable
from typing import Any

from adam.storage import SessionStore
from adam.tool_calls.command_tools import CommandLogError
from adam.tool_calls.registry import ToolRegistry


class ToolExecutor:
    """协调日志与工具调用；工具本身的参数、权限和确认由注册表处理。"""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry,
        notify: Callable[[str], None],
    ) -> None:
        self.store = store
        self.registry = registry
        self.notify = notify

    def execute(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """串行执行 calls，返回与调用 ID 配对的 tool 消息。

        turn_id 和 request_id 指向产生这些工具调用的轮次与模型请求。
        开始记录写入后才执行，结果落盘后才返回；存储错误直接向上抛，
        禁止模型在执行轨迹无法保存时继续行动。
        """
        results = []
        for call in calls:
            link = {
                "turn_id": turn_id,
                "request_id": request_id,
                "tool_call_id": call["id"],
            }
            output = self._execute_call(session_id, link, call["function"])
            results.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        return results

    def _execute_call(self, session_id: str, link: dict[str, str], function: dict[str, Any]) -> str:
        """保留原始参数和实际回传结果；命令还记录审批及进程执行边界。"""
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
            except ValueError as error:
                # 日志关联错误不能被注册表当成模型参数错误后继续执行。
                raise CommandLogError("命令执行日志校验失败；已停止本轮，未自动重试。") from error

        self.notify(f"工具> {function['name']}")
        # 通用开始事件表示进入工具处理；命令的审批与进程结果另由 command_* 记录。
        output = self.registry.execute(
            function["name"], function["arguments"], record_execution=record_command
        )
        self.store.append_tool_event(session_id, "tool_finished", {**link, "output": output})
        return output

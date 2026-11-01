"""显式 API 的工具桥；脚本不能自行扩展注册表或绕过原参数、模式和路径检查。"""

import json
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.models import ExecutionRecorder
from adam.tool_calls.registry import BeforeTool, ToolRegistry


class RegisteredToolBridge:
    """只转交宿主明确列出的已注册工具；聊天的预算和日志接入另由编排调用方负责。"""

    def __init__(
        self,
        registry: ToolRegistry,
        allowed: tuple[str, ...],
        *,
        cancel: CancellationToken | None = None,
        record_execution: ExecutionRecorder | None = None,
        before_tool: BeforeTool | None = None,
    ) -> None:
        registered = {item["function"]["name"] for item in registry.definitions()}
        if (
            not allowed
            or len(allowed) > 64
            or len(set(allowed)) != len(allowed)
            or set(allowed) - registered
        ):
            raise ValueError("编排必须显式指定不重复的已注册工具。")
        self.registry, self.allowed = registry, frozenset(allowed)
        self.cancel = cancel or CancellationToken()
        self.record_execution, self.before_tool = record_execution, before_tool

    def __call__(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具拒绝作为原有 JSON 返回；取消与日志故障保持异常，不自行重复执行。"""
        self.cancel.check()
        if name not in self.allowed:
            return {"ok": False, "error": "该工具未向本次编排开放。"}
        result = json.loads(
            self.registry.execute(
                name,
                json.dumps(arguments, ensure_ascii=False, allow_nan=False),
                record_execution=self.record_execution,
                before_tool=self.before_tool,
                cancel=self.cancel,
            )
        )
        if not isinstance(result, dict):
            raise ValueError("注册工具返回格式无效。")
        return result

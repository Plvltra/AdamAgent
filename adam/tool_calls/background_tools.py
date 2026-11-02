"""主 Agent 的后台命令入口；会话和请求来源由宿主绑定。"""

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import SPECS, schema
from adam.tool_calls.registry import ToolRegistry


class BackgroundTools:
    """后台能力只注册主 Agent；复用原执行器的模式、Hook、额度和工具事件。"""

    def __init__(
        self,
        start_task: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]],
        query_task: Callable[..., dict[str, Any]],
        store: SessionStore,
    ) -> None:
        self._start_task, self._query_task, self.store = start_task, query_task, store
        self._source: dict[str, str] = {}

    def bind_source(self, source: dict[str, str]) -> None:
        """由执行器逐次绑定真实调用来源，模型不能提供会话归属。"""
        self._source = dict(source)

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        """启动属于执行能力；查询串行运行，取消也只能操作当前会话任务。"""
        start = deepcopy(SPECS["run_command"])
        start["description"] = (
            "启动需要逐次批准的宿主后台命令，立即返回task_id；不是沙箱。用background_task续读、等待或取消。退出码未知不能当成成功，退出应用会停止任务。"
        )
        query = schema(
            "查询当前会话后台任务，不重新执行或批准。read/wait/cancel；每流使用上次next_cursor。恢复记录只有旧快照，interrupted表示结果未知。",
            {
                "task_id": ("string", "启动返回的任务ID"),
                "action": ("string", "read（默认）、wait、cancel"),
                "stdout_cursor": ("integer", "stdout字节游标，默认0"),
                "stderr_cursor": ("integer", "stderr字节游标，默认0"),
                "seconds": ("integer", "等待0～10秒，默认1"),
            },
            ["task_id"],
        )
        return registry.extended(
            {"start_background_command": start, "background_task": query},
            {"start_background_command": self.start, "background_task": self.query},
            capabilities={
                "start_background_command": ToolCapability.RUN_PROCESS,
                "background_task": ToolCapability.SESSION_STATE,
            },
            groups={"start_background_command": "commands", "background_task": "commands"},
            path_selectors={"start_background_command": lambda args: (Path(args.get("cwd", ".")),)},
        )

    def start(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """先写独立任务意图；通用工具日志由执行器保存，不伪造同步command_finished。"""
        return self._start_task(self._owner(), args, self._source)

    def query(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """只返回一小页，完整有界快照留在侧文件，避免常规预览抹掉续读游标。"""
        return self._query_task(self._owner(), **args)

    def _owner(self) -> str:
        owner = self.store.active_session_id
        if owner is None or not self._source:
            raise ToolError("后台任务缺少宿主会话或调用来源。")
        return owner

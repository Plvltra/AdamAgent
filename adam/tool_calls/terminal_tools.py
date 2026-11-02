"""主Agent持久管道终端工具；身份来自执行器，启动和输入仍分别批准。"""

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


class TerminalTools:
    """注入宿主协调回调；工具层不持有进程或文件锁，也不允许模型选择会话。"""

    def __init__(
        self,
        start: Callable[..., dict[str, Any]],
        send: Callable[..., dict[str, Any]],
        query: Callable[..., dict[str, Any]],
        stop: Callable[..., dict[str, Any]],
        store: SessionStore,
    ) -> None:
        self._start, self._send, self._query, self._stop = start, send, query, stop
        self.store, self._source = store, {}

    def bind_source(self, source: dict[str, str]) -> None:
        """执行器逐次绑定轮次/请求/工具来源。"""
        self._source = dict(source)

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        """输入属于执行能力，plan模式禁止；读/关闭只作用于当前会话已有终端。"""
        start = deepcopy(SPECS["run_command"])
        start["description"] = (
            "批准后启动独立持久管道终端，保留进程cwd/env。argv显式选择shell或行交互程序，"
            "不是TTY或沙箱；timeout_seconds限制整个进程寿命。terminal_input每次输入另行批准。"
        )
        start["parameters"]["properties"]["env"] = {
            "type": "object",
            "description": "显式环境覆盖，字符串键值，须批准",
            "additionalProperties": {"type": "string"},
        }
        identity = {"terminal_id": ("string", "open_terminal返回的ID")}
        specs = {
            "open_terminal": start,
            "terminal_input": schema(
                "原样UTF-8输入，每次须批准，需换行请显式包含换行；accepted/written均不代表执行成功。",
                {
                    **identity,
                    "text": ("string", "最多8192字节"),
                    "eof": ("boolean", "写完关闭stdin，默认false"),
                },
                ["terminal_id", "text"],
            ),
            "read_terminal": schema(
                "读取有界双流，按next_cursor续读。exit_code属于终端进程，不表示每条输入命令结果。",
                {
                    **identity,
                    "stdout_cursor": ("integer", "stdout字节游标默认0"),
                    "stderr_cursor": ("integer", "stderr字节游标默认0"),
                },
                ["terminal_id"],
            ),
            "close_terminal": schema(
                "取消并回收当前会话指定终端，不按旧PID重连。", identity, ["terminal_id"]
            ),
        }
        return registry.extended(
            specs,
            {
                "open_terminal": self.start,
                "terminal_input": self.send,
                "read_terminal": self.query,
                "close_terminal": self.stop,
            },
            capabilities={
                name: ToolCapability.RUN_PROCESS
                if name in {"open_terminal", "terminal_input"}
                else ToolCapability.SESSION_STATE
                for name in specs
            },
            groups={name: "commands" for name in specs},
            path_selectors={"open_terminal": lambda args: (Path(args.get("cwd", ".")),)},
        )

    def start(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """固定当前会话及真实来源后启动。"""
        return self._start(self._owner(), args, self._source)

    def send(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """输入正文及EOF都在宿主批准计划内。"""
        return self._send(
            self._owner(),
            args["terminal_id"],
            args["text"],
            self._source,
            eof=args.get("eof", False),
        )

    def query(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """查询不重新执行，不申请新的执行授权。"""
        return self._query(
            self._owner(),
            args["terminal_id"],
            stdout_cursor=args.get("stdout_cursor", 0),
            stderr_cursor=args.get("stderr_cursor", 0),
        )

    def stop(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """只取消指定会话内的终端。"""
        return self._stop(self._owner(), args["terminal_id"])

    def _owner(self) -> str:
        owner = self.store.active_session_id
        if owner is None or not self._source.get("turn_id"):
            raise ToolError("终端操作缺少当前会话或执行来源。")
        return owner

"""复用一个 stdio 连接；异步上下文由同一 AnyIO 任务进入和退出。"""

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import Client, StdioServerParameters

from adam.config.mcp import MCPServerConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError


class MCPConnection:
    """显式 open/close 管理进程；调用失败不自动重发，不把服务器声明当沙箱。"""

    def __init__(self, config: MCPServerConfig, workspace: Path) -> None:
        self.config, self.workspace = config, workspace
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None
        self._client: Client | None = None
        self._tools: list[dict[str, Any]] = []

    def open(self) -> "MCPConnection":
        """启动可信程序并完成握手与目录发现；任何失败都会回收已创建资源。"""
        if self._stack is not None:
            raise ToolError("MCP 连接已经打开。")
        stack = ExitStack()
        try:
            self._portal = stack.enter_context(start_blocking_portal())
            client = Client(
                StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    cwd=str(self.workspace),
                ),
                read_timeout_seconds=float(self.config.timeout_seconds),
            )
            self._client = stack.enter_context(self._portal.wrap_async_context_manager(client))
            result = self._portal.call(self._client.list_tools)
            if result.next_cursor is not None or len(result.tools) > 64:
                raise ToolError("MCP 工具目录过大；本版不自动遍历更多页。")
            self._tools = [tool.model_dump(mode="json") for tool in result.tools]
        except BaseException as error:
            try:
                stack.close()
            except Exception:
                error.add_note("MCP 启动失败后的资源清理异常。")
            self._client = self._portal = None
            if isinstance(error, (KeyboardInterrupt, ToolError)):
                raise
            raise ToolError(f"MCP {self.config.alias} 启动或目录发现失败；未重试。") from error
        self._stack = stack
        return self

    def definitions(self) -> list[dict[str, Any]]:
        """返回启动时目录；不为每次工具调用重新启动进程。"""
        from copy import deepcopy

        return deepcopy(self._tools)

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        cancel: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """一次本地执行对应一次协议调用；取消只停止等待，随后关闭连接回收进程。"""
        if self._portal is None or self._client is None:
            raise ToolError("MCP 连接已关闭。")
        if cancel:
            cancel.check()
        future = self._portal.start_task_soon(self._call, name, arguments)
        try:
            return self._wait(future, cancel, self.config.timeout_seconds + 1)
        except BaseException as error:
            future.cancel()
            try:
                self.close()
            except Exception:
                error.add_note("MCP 连接清理失败；原始错误保留。")
            raise

    def close(self) -> None:
        """回收连接、服务进程与事件线程；可重复调用，不创建新连接。"""
        stack, self._stack = self._stack, None
        self._client = self._portal = None
        if stack:
            stack.close()

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """低层 session.call_tool 避免高层 HEADER_MISMATCH 自动重发。"""
        assert self._client is not None
        try:
            result = await self._client.session.call_tool(
                name,
                arguments,
                read_timeout_seconds=float(self.config.timeout_seconds),
            )
            text = [item.text for item in result.content if item.type == "text"]
            if len(text) != len(result.content):
                raise ToolError("本版 MCP 仅接收文本工具结果。")
            return {
                "ok": not result.is_error,
                "content": text,
                "structured_content": result.structured_content,
                **({"error": "MCP 服务报告工具失败。"} if result.is_error else {}),
            }
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(
                f"MCP {self.config.alias} 调用失败（超时、断开或协议错误）；未自动重发。"
            ) from error

    @staticmethod
    def _wait(
        future: Future[dict[str, Any]], cancel: CancellationToken | None, seconds: float
    ) -> dict[str, Any]:
        """短轮询允许主线程 Ctrl+C 或子任务取消，避免无限阻塞终端。"""
        deadline = monotonic() + seconds
        while True:
            if monotonic() >= deadline:
                raise ToolError("MCP 调用超过总等待时限；未自动重发。")
            if cancel:
                cancel.check()
            try:
                return future.result(timeout=0.05)
            except FutureTimeout:
                continue

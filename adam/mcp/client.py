"""复用一个 stdio 连接；异步上下文由同一 AnyIO 任务进入和退出。"""

from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import Client, StdioServerParameters
from mcp.types import GetPromptResult, PaginatedRequestParams, ReadResourceResult

from adam.config.mcp import MCPServerConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.mcp import prompts
from adam.mcp.resources import MAX_RESOURCE_BYTES, catalog, text_result, validate_read


class MCPConnection:
    """显式 open/close 管理进程；调用失败不自动重发，不把服务器声明当沙箱。"""

    def __init__(self, config: MCPServerConfig, workspace: Path) -> None:
        self.config, self.workspace = config, workspace
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None
        self._client: Client | None = None
        self._tools: list[dict[str, Any]] = []
        self._resources: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []

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
            if self.config.tools:
                result = self._portal.call(self._client.list_tools)
                if result.next_cursor is not None or len(result.tools) > 64:
                    raise ToolError("MCP 工具目录过大；本版不自动遍历更多页。")
                self._tools = [tool.model_dump(mode="json") for tool in result.tools]
            if self.config.resources:
                self.list_resources()
            if self.config.prompts:
                self.list_prompts()
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
        return self._run(lambda: self._call(name, arguments), cancel)

    def list_resources(self, cancel: CancellationToken | None = None) -> dict[str, Any]:
        """刷新最多四页目录，仅公开配置允许的固定 URI；不读取资源正文或展开模板。"""
        if not self.config.resources:
            raise ToolError("此 MCP 服务未配置资源读取范围。")
        result = self._run(self._list_resources, cancel)
        self._resources = result["resources"]
        from copy import deepcopy

        return deepcopy(result)

    def read_resource(
        self,
        uri: str,
        expected_version: str | None = None,
        cancel: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """读取允许 URI 的完整有界文本；可要求与上次内容版本一致，变化时不返回新正文。"""
        validate_read(uri, expected_version, self.config.resources)
        item = next((item for item in self._resources if item["uri"] == uri), None)
        if item is None or (
            item["declared_size"] is not None and item["declared_size"] > MAX_RESOURCE_BYTES
        ):
            raise ToolError("MCP 资源未确认在目录中或声明大小超过限制。")
        return self._run(lambda: self._read_resource(uri, expected_version), cancel)

    def list_prompts(self, cancel: CancellationToken | None = None) -> dict[str, Any]:
        """刷新允许模板的有限目录与参数声明；不预取模板内容或改变会话提示。"""
        if not self.config.prompts:
            raise ToolError("此 MCP 服务未配置提示模板范围。")
        result = self._run(self._list_prompts, cancel)
        self._prompts = result["prompts"]
        from copy import deepcopy

        return deepcopy(result)

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
        cancel: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """校验参数后读取完整模板；结果只含带来源的资料，消息角色不会提升权限。"""
        checked = prompts.validate_arguments(name, arguments, self.config.prompts, self._prompts)
        return self._run(lambda: self._get_prompt(name, checked), cancel)

    def close(self) -> None:
        """回收连接、服务进程与事件线程；可重复调用，不创建新连接。"""
        stack, self._stack = self._stack, None
        self._client = self._portal = None
        if stack:
            stack.close()

    def _run(
        self,
        operation: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
        cancel: CancellationToken | None,
    ) -> dict[str, Any]:
        """工具和资料共用总等待及清理；失败取消 Future 并关闭连接，不自动重试。"""
        if self._portal is None or self._client is None:
            raise ToolError("MCP 连接已关闭。")
        if cancel:
            cancel.check()
        future = self._portal.start_task_soon(operation)
        try:
            return self._wait(future, cancel, self.config.timeout_seconds + 1)
        except BaseException as error:
            future.cancel()
            try:
                self.close()
            except Exception:
                error.add_note("MCP 连接清理失败；原始错误保留。")
            raise

    async def _list_resources(self) -> dict[str, Any]:
        """低层 session 逐页读取一次，拒绝循环游标；不使用 SDK 的缓存或自动重新请求。"""
        assert self._client is not None
        cursor, seen, items = None, set(), []
        try:
            for _ in range(4):
                result = await self._client.session.list_resources(
                    params=PaginatedRequestParams(cursor=cursor)
                )
                items.extend(item.model_dump(mode="json") for item in result.resources)
                if len(items) > 64:
                    raise ToolError("MCP 资源目录超过64项。")
                cursor = result.next_cursor
                if cursor is None:
                    return {
                        "ok": True,
                        "server": self.config.alias,
                        "resources": catalog(items, self.config.resources),
                    }
                if not cursor or len(cursor) > 1024 or cursor in seen:
                    raise ToolError("MCP 资源分页游标重复或无效。")
                seen.add(cursor)
            raise ToolError("MCP 资源目录超过四页，未返回残缺目录。")
        except ToolError:
            raise
        except Exception as error:
            raise ToolError("MCP 资源目录读取失败；未重试。") from error

    async def _read_resource(self, uri: str, expected_version: str | None) -> dict[str, Any]:
        """只接受已完成的资源响应，不替服务回答交互请求或跟随 URI 发起额外 IO。"""
        assert self._client is not None
        try:
            result = await self._client.session.read_resource(uri)
            if not isinstance(result, ReadResourceResult):
                raise ToolError("MCP 资源未返回完整内容。")
            return {
                **text_result(result.model_dump(mode="json"), uri, expected_version),
                "server": self.config.alias,
            }
        except ToolError:
            raise
        except Exception as error:
            raise ToolError("MCP 资源读取失败（超时、断开或协议错误）；未自动重发。") from error

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

    async def _list_prompts(self) -> dict[str, Any]:
        """读取最多四页声明；循环游标、超限和缺失均失败，不使用 SDK 自动重试。"""
        assert self._client is not None
        cursor, seen, items = None, set(), []
        try:
            for _ in range(4):
                result = await self._client.session.list_prompts(
                    params=PaginatedRequestParams(cursor=cursor)
                )
                items.extend(item.model_dump(mode="json") for item in result.prompts)
                if len(items) > 64:
                    raise ToolError("MCP 提示目录超过64项。")
                cursor = result.next_cursor
                if cursor is None:
                    return {
                        "ok": True,
                        "server": self.config.alias,
                        "prompts": prompts.catalog(items, self.config.prompts),
                    }
                if not cursor or len(cursor) > 1024 or cursor in seen:
                    raise ToolError("MCP 提示分页游标重复或无效。")
                seen.add(cursor)
            raise ToolError("MCP 提示目录超过四页，未返回残缺目录。")
        except ToolError:
            raise
        except Exception as error:
            raise ToolError("MCP 提示目录读取失败；未重试。") from error

    async def _get_prompt(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        """一次获取对应一次低层请求；拒绝交互续传、非文本或协议错误，不拼接角色消息。"""
        assert self._client is not None
        try:
            result = await self._client.session.get_prompt(name, arguments)
            if not isinstance(result, GetPromptResult):
                raise ToolError("MCP 提示未返回完整内容。")
            return {
                **prompts.text_result(result.model_dump(mode="json"), name),
                "server": self.config.alias,
            }
        except ToolError:
            raise
        except Exception as error:
            raise ToolError("MCP 提示获取失败（超时、断开或协议错误）；未自动重发。") from error

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

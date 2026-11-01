"""只在连接期读取认证环境变量；HTTP 固定端点、TLS 校验且不继承代理配置。"""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from adam.config.mcp import MCPServerConfig
from adam.contracts.errors import ToolError


@asynccontextmanager
async def open_client(config: MCPServerConfig, workspace: Path) -> AsyncIterator[Client]:
    """同一异步任务创建并关闭传输；远程不启动进程，stdio 保留原最小环境和回收行为。

    token_env 只用于远程 Bearer 认证，不写回配置、模型资料或会话记录。
    HTTP 关闭只停止本地等待与连接，不能证明远端已停止；不会重发业务 POST。
    """
    timeout = float(config.timeout_seconds)
    if config.url is None:
        async with Client(
            StdioServerParameters(
                command=config.command, args=list(config.args), cwd=str(workspace)
            ),
            read_timeout_seconds=timeout,
        ) as client:
            yield client
        return
    endpoint = httpx2.URL(config.url)

    async def check_request(request: httpx2.Request) -> None:
        # SDK 自行处理重定向，不受 follow_redirects=False 约束；每次发送再核对精确端点。
        if request.url != endpoint:
            raise ToolError("MCP 请求离开配置端点；未发送。")

    async def check_response(response: httpx2.Response) -> None:
        if response.is_redirect:
            raise ToolError("MCP 端点返回重定向；请在配置中明确最终地址。")

    async with httpx2.AsyncClient(
        headers=_headers(config.token_env),
        timeout=httpx2.Timeout(timeout),
        verify=True,
        trust_env=False,
        follow_redirects=False,
        event_hooks={"request": [check_request], "response": [check_response]},
    ) as http:
        async with Client(
            streamable_http_client(config.url, http_client=http, max_sse_event_size=1048576),
            read_timeout_seconds=timeout,
        ) as client:
            yield client


def _headers(variable: str | None) -> dict[str, str]:
    """只接受有界 Bearer token；缺失或非法时在连接前失败，错误不包含变量值。"""
    if variable is None:
        return {}
    token = os.environ.get(variable)
    if not token or len(token) > 4096 or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None:
        raise ToolError("MCP 认证环境变量缺失或不是有效 Bearer token；未连接。")
    return {"Authorization": f"Bearer {token}"}

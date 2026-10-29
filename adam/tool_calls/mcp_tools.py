"""注册明确启用的 MCP 只读工具；生命周期交给组装方的 ExitStack。"""

import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from adam.config.mcp import MCPConfig
from adam.contracts.errors import ToolError
from adam.contracts.models import ExecutionRecorder
from adam.mcp.client import MCPConnection
from adam.mcp.schema import validator_for
from adam.tool_calls.registry import ToolHandler, ToolRegistry


def register_mcp(
    registry: ToolRegistry,
    config: MCPConfig,
    workspace: Path,
    resources: ExitStack,
) -> ToolRegistry:
    """启动配置中的可信服务；名字隔离、明确工具白名单和只读声明缺一不可。"""
    if not config.enabled:
        return registry
    for server in config.servers:
        connection = MCPConnection(server, workspace).open()
        resources.callback(connection.close)
        available = connection.definitions()
        names = [tool["name"] for tool in available]
        if len(names) != len(set(names)) or not set(server.tools) <= set(names):
            raise ToolError("MCP 工具重复或配置允许的工具未提供。")
        for tool in available:
            if tool["name"] not in server.tools:
                continue
            name = f"mcp_{server.alias}_{tool['name']}"
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise ToolError("MCP 映射后的工具名无效。")
            if (tool.get("annotations") or {}).get("read_only_hint") is not True:
                raise ToolError("本版只接入明确声明只读的 MCP 工具。")
            schema = tool["input_schema"]
            if schema.get("type") != "object":
                raise ToolError("MCP 工具参数必须是对象。")
            description = tool.get("description") or "已配置的本地只读查询。"
            if len(description) > 2000:
                raise ToolError("MCP 工具说明过长。")
            try:
                registry = registry.extended(
                    {name: {"description": description, "parameters": schema}},
                    {name: _handler(connection, tool["name"])},
                    validators={name: validator_for(schema)},
                )
            except ValueError as error:
                raise ToolError("MCP 工具名称冲突；未覆盖已有工具。") from error
    return registry


def _handler(connection: MCPConnection, name: str) -> ToolHandler:
    """固定连接和原始工具名；模型只能传 Schema 允许的业务参数。"""

    def call(arguments: dict[str, Any], _record: ExecutionRecorder | None) -> dict[str, Any]:
        return connection.call(name, arguments)

    return call

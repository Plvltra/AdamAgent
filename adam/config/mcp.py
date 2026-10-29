"""本地 MCP 服务的显式启动配置；模型不能自行指定程序或扩展权限。"""

import math
import re
from dataclasses import dataclass

from adam.contracts.errors import ConfigError


@dataclass(frozen=True)
class MCPServerConfig:
    """只启动配置允许的本地程序；tools 为显式接受的只读工具名。"""

    alias: str
    command: str
    args: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        """直接构造也校验名字、程序参数和超时，不能用布尔值充当秒数。"""
        if not isinstance(self.alias, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", self.alias):
            raise ConfigError("MCP alias 必须为1～16位小写字母、数字或下划线。")
        if not isinstance(self.command, str) or not self.command.strip() or "\0" in self.command:
            raise ConfigError("MCP command 必须是有效程序名。")
        if (
            not isinstance(self.args, tuple)
            or len(self.args) > 32
            or any(not isinstance(x, str) or "\0" in x or len(x) > 4000 for x in self.args)
        ):
            raise ConfigError("MCP args 必须是不超过32个有界字符串。")
        if (
            not isinstance(self.tools, tuple)
            or not 1 <= len(self.tools) <= 16
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", x)
                for x in self.tools
            )
            or len(set(self.tools)) != len(self.tools)
        ):
            raise ConfigError("MCP tools 必须是1～16个不重复工具名。")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= 60
        ):
            raise ConfigError("MCP timeout_seconds 必须在0.1～60秒之间。")


@dataclass(frozen=True)
class MCPConfig:
    """默认不启动服务；启用最多四个别名不同的受信本地服务。"""

    enabled: bool = False
    servers: tuple[MCPServerConfig, ...] = ()

    def __post_init__(self) -> None:
        """限制服务数量和别名唯一性，关闭配置也保持有效结构。"""
        if type(self.enabled) is not bool or not isinstance(self.servers, tuple):
            raise ConfigError("MCP 配置类型错误。")
        if len(self.servers) > 4 or any(not isinstance(s, MCPServerConfig) for s in self.servers):
            raise ConfigError("MCP 最多配置4个服务。")
        if len({s.alias for s in self.servers}) != len(self.servers):
            raise ConfigError("MCP 服务别名重复。")


def parse_mcp(value: object) -> MCPConfig:
    """严格解析 TOML；默认关闭时也检查配置结构，不隐式连接远程服务。"""
    if not isinstance(value, dict) or value.keys() - {"enabled", "servers"}:
        raise ConfigError("mcp 仅支持 enabled 和 servers。")
    entries = value.get("servers", [])
    if not isinstance(entries, list):
        raise ConfigError("mcp.servers 必须是表数组。")
    servers = []
    for item in entries:
        if (
            not isinstance(item, dict)
            or item.keys() - {"alias", "command", "args", "tools", "timeout_seconds"}
            or not {"alias", "command", "tools"} <= item.keys()
        ):
            raise ConfigError("MCP 服务字段缺失或存在未知字段。")
        if not isinstance(item.get("args", []), list) or not isinstance(item["tools"], list):
            raise ConfigError("MCP args 和 tools 必须是数组。")
        servers.append(
            MCPServerConfig(
                item["alias"],
                item["command"],
                tuple(item.get("args", [])),
                tuple(item["tools"]),
                item.get("timeout_seconds", 10),
            )
        )
    return MCPConfig(value.get("enabled", False), tuple(servers))

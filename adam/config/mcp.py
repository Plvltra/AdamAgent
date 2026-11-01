"""MCP 本地程序或远程端点的显式配置；模型不能选择连接目的地或扩展权限。"""

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from adam.contracts.errors import ConfigError


@dataclass(frozen=True)
class MCPServerConfig:
    """只连接配置允许的服务；只读工具、固定资源 URI 和模板分别明确允许范围。"""

    alias: str
    command: str = ""
    args: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    timeout_seconds: float = 10
    resources: tuple[str, ...] = ()
    prompts: tuple[str, ...] = ()
    url: str | None = None
    token_env: str | None = None

    def __post_init__(self) -> None:
        """直接构造也校验名字、程序参数和超时，不能用布尔值充当秒数。"""
        if not isinstance(self.alias, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", self.alias):
            raise ConfigError("MCP alias 必须为1～16位小写字母、数字或下划线。")
        if not isinstance(self.command, str) or "\0" in self.command:
            raise ConfigError("MCP command 必须是有效程序名。")
        if (
            not isinstance(self.args, tuple)
            or len(self.args) > 32
            or any(not isinstance(x, str) or "\0" in x or len(x) > 4000 for x in self.args)
        ):
            raise ConfigError("MCP args 必须是不超过32个有界字符串。")
        self._validate_transport()
        if (
            not isinstance(self.tools, tuple)
            or len(self.tools) > 16
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", x)
                for x in self.tools
            )
            or len(set(self.tools)) != len(self.tools)
        ):
            raise ConfigError("MCP tools 必须是不超过16个不重复工具名。")
        if (
            not isinstance(self.resources, tuple)
            or len(self.resources) > 16
            or any(
                not isinstance(uri, str)
                or not 1 <= len(uri) <= 2048
                or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", uri) is None
                or any(ord(char) < 32 or ord(char) == 127 for char in uri)
                for uri in self.resources
            )
            or len(set(self.resources)) != len(self.resources)
        ):
            raise ConfigError("MCP resources 必须是不超过16个不重复的有界绝对 URI。")
        if (
            not isinstance(self.prompts, tuple)
            or len(self.prompts) > 16
            or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", name)
                for name in self.prompts
            )
            or len(set(self.prompts)) != len(self.prompts)
        ):
            raise ConfigError("MCP prompts 必须是不超过16个不重复模板名。")
        if not self.tools and not self.resources and not self.prompts:
            raise ConfigError("MCP 服务须明确配置至少一个 tool、resource 或 prompt。")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= 60
        ):
            raise ConfigError("MCP timeout_seconds 必须在0.1～60秒之间。")

    def _validate_transport(self) -> None:
        """程序与 URL 二选一；远程只允许 HTTPS，明文仅供数值回环地址的本地练习。"""
        if self.url is None:
            if not self.command.strip() or self.token_env is not None:
                raise ConfigError("stdio MCP 须指定 command，不能配置远程 token_env。")
            return
        if self.command or self.args:
            raise ConfigError("MCP URL 与 command/args 不能同时配置。")
        if (
            not isinstance(self.url, str)
            or not 1 <= len(self.url) <= 2048
            or any(ord(char) <= 32 or ord(char) >= 127 for char in self.url)
            or "\\" in self.url
        ):
            raise ConfigError("MCP URL 必须是有界 ASCII 端点。")
        try:
            parsed = urlsplit(self.url)
            valid = (
                bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and (
                    parsed.scheme == "https"
                    or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"})
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("MCP URL 须为 HTTPS 或数值回环 HTTP，不能含账号、查询参数或片段。")
        if self.token_env is not None and (
            not isinstance(self.token_env, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", self.token_env) is None
        ):
            raise ConfigError("MCP token_env 必须是最多64字符的环境变量名。")


@dataclass(frozen=True)
class MCPConfig:
    """默认不连接服务；启用最多四个别名不同的受信服务。"""

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
    """严格解析 TOML；默认关闭时也检查配置结构，仅显式 URL 才选择远程传输。"""
    if not isinstance(value, dict) or value.keys() - {"enabled", "servers"}:
        raise ConfigError("mcp 仅支持 enabled 和 servers。")
    entries = value.get("servers", [])
    if not isinstance(entries, list):
        raise ConfigError("mcp.servers 必须是表数组。")
    servers = []
    for item in entries:
        if (
            not isinstance(item, dict)
            or item.keys()
            - {
                "alias",
                "command",
                "args",
                "tools",
                "timeout_seconds",
                "resources",
                "prompts",
                "url",
                "token_env",
            }
            or "alias" not in item
        ):
            raise ConfigError("MCP 服务字段缺失或存在未知字段。")
        if any(
            not isinstance(item.get(name, []), list)
            for name in ("args", "tools", "resources", "prompts")
        ):
            raise ConfigError("MCP args、tools、resources 和 prompts 必须是数组。")
        servers.append(
            MCPServerConfig(
                item["alias"],
                item.get("command", ""),
                tuple(item.get("args", [])),
                tuple(item.get("tools", [])),
                item.get("timeout_seconds", 10),
                tuple(item.get("resources", [])),
                tuple(item.get("prompts", [])),
                item.get("url"),
                item.get("token_env"),
            )
        )
    return MCPConfig(value.get("enabled", False), tuple(servers))

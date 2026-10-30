"""用户明确配置的本地工具检查器；默认关闭，模型参数不能指定检查程序。"""

import math
from dataclasses import dataclass, fields
from typing import Any

from adam.contracts.errors import ConfigError


@dataclass(frozen=True)
class HookCommand:
    """检查器启动参数；在工具工作目录运行，不经过隐式 shell。"""

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        """校验 argv 的类型与大小，拒绝隐式批处理启动。"""
        if (
            not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= 32
            or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 4000 for arg in self.argv)
            or not self.argv[0].strip()
            or sum(len(arg) for arg in self.argv) > 16000
        ):
            raise ConfigError("Hook argv 必须为1～32个有界字符串，程序名不能为空。")
        if self.argv[0].lower().endswith((".bat", ".cmd")):
            raise ConfigError("Hook 批处理必须显式通过 cmd.exe 指定，不能隐式执行 shell。")


@dataclass(frozen=True)
class HooksConfig:
    """每种事件最多一个检查器；输入/每条输出流分别限制字节，超时单位为秒。"""

    enabled: bool = False
    before_tool: HookCommand | None = None
    after_tool: HookCommand | None = None
    timeout_seconds: float = 5
    max_input_bytes: int = 65536
    max_output_bytes: int = 8192

    def __post_init__(self) -> None:
        """直接构造与 TOML 使用相同限制，布尔值不能冒充超时或容量。"""
        if type(self.enabled) is not bool:
            raise ConfigError("hooks.enabled 必须是布尔值。")
        if any(
            value is not None and not isinstance(value, HookCommand)
            for value in (self.before_tool, self.after_tool)
        ):
            raise ConfigError("Hook 事件必须使用有效的 argv 配置。")
        if self.enabled and self.before_tool is None and self.after_tool is None:
            raise ConfigError("启用 Hooks 时至少配置 before_tool 或 after_tool。")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= 60
        ):
            raise ConfigError("hooks.timeout_seconds 必须在0.1～60秒之间。")
        for value, name, maximum in (
            (self.max_input_bytes, "max_input_bytes", 1048576),
            (self.max_output_bytes, "max_output_bytes", 65536),
        ):
            if type(value) is not int or not 256 <= value <= maximum:
                raise ConfigError(f"hooks.{name} 必须在256～{maximum}字节之间。")


def parse_hooks(value: object) -> HooksConfig:
    """解析 TOML，关闭时也检查字段；不启动程序或读取模型连接配置。"""
    if not isinstance(value, dict) or value.keys() - {item.name for item in fields(HooksConfig)}:
        raise ConfigError("hooks 包含未知字段或不是配置表。")
    values: dict[str, Any] = dict(value)
    for event in ("before_tool", "after_tool"):
        if event not in values:
            continue
        item = values[event]
        if (
            not isinstance(item, dict)
            or item.keys() != {"argv"}
            or not isinstance(item["argv"], list)
        ):
            raise ConfigError(f"hooks.{event} 必须只包含 argv 数组。")
        values[event] = HookCommand(tuple(item["argv"]))
    return HooksConfig(**values)

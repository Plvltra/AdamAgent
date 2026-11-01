"""宿主提供的只读执行状态；不读取模型内容、不预留额度，也不授予工具权限。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adam.contracts.execution import ExecutionMode

RUNTIME_STATE_VERSION = "runtime_state_v1"


def _count(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 必须为非负整数。")


@dataclass(frozen=True)
class CountState:
    """本轮计数；remaining 包含尚未开始的下一次，实际超额 used 不截掉。"""

    maximum: int
    used: int

    def __post_init__(self) -> None:
        _count(self.maximum, "maximum")
        _count(self.used, "used")

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    def as_record(self) -> dict[str, int]:
        """返回独立字典，修改它不会改变快照。"""
        return {"maximum": self.maximum, "used": self.used, "remaining": self.remaining}


@dataclass(frozen=True)
class TokenState:
    """整组已结算消费和在途估算预留；余量不是可独占的额度或费用保证。

    known_used 始终是已知小计。unknown_usage 表示还有无法确定的消费；reserved
    为 None 表示预留未知。任一项未知时 remaining 为 None，不能按零消费处理。
    """

    maximum: int
    known_used: int
    unknown_usage: bool
    reserved: int | None

    def __post_init__(self) -> None:
        _count(self.maximum, "maximum")
        _count(self.known_used, "known_used")
        if type(self.unknown_usage) is not bool:
            raise ValueError("unknown_usage 必须为布尔值。")
        if self.reserved is not None:
            _count(self.reserved, "reserved")

    @property
    def remaining(self) -> int | None:
        if self.unknown_usage or self.reserved is None:
            return None
        return max(0, self.maximum - self.known_used - self.reserved)

    def as_record(self) -> dict[str, int | bool | None]:
        """同时输出基础值和派生余量，未知不会抹去已有消费。"""
        return {
            "maximum": self.maximum,
            "known_used": self.known_used,
            "unknown_usage": self.unknown_usage,
            "reserved": self.reserved,
            "remaining": self.remaining,
        }


@dataclass(frozen=True)
class FailureState:
    """连续相同工具失败的观测；None 表示未观察，0 不代表任务语义成功。"""

    consecutive: int | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.consecutive is not None:
            _count(self.consecutive, "consecutive")
        if not self.consecutive:
            if self.fingerprint is not None:
                raise ValueError("没有连续失败时不能携带失败指纹。")
        elif (
            not isinstance(self.fingerprint, str)
            or len(self.fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in self.fingerprint)
        ):
            raise ValueError("连续失败需要宿主观察产生的 SHA-256 指纹。")

    def as_record(self) -> dict[str, int | str | None]:
        """只公开计数与摘要，不复制工具参数、错误正文或文件内容。"""
        return {"consecutive": self.consecutive, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class RuntimeState:
    """一次输入构造可使用的宿主状态；各来源并非全局原子快照。

    workspace/mode 必须由宿主提供，不从模型回答解析。pending_agents 为 None
    表示未知，空元组表示确认为无；ID 仅用于关联，不表示子任务已经成功。
    本对象不读写文件、不占用额度、不改变实际执行规则。
    """

    requests: CountState
    tools: CountState
    tokens: TokenState
    failures: FailureState
    workspace: str
    mode: ExecutionMode
    pending_agents: tuple[str, ...] | None

    def __post_init__(self) -> None:
        for value, expected in (
            (self.requests, CountState),
            (self.tools, CountState),
            (self.tokens, TokenState),
            (self.failures, FailureState),
            (self.mode, ExecutionMode),
        ):
            if not isinstance(value, expected):
                raise ValueError("执行状态需使用已校验的值对象。")
        if (
            not isinstance(self.workspace, str)
            or "\0" in self.workspace
            or not Path(self.workspace).is_absolute()
        ):
            raise ValueError("工作目录需为宿主提供的绝对路径。")
        pending = self.pending_agents
        if pending is not None and (
            not isinstance(pending, tuple)
            or any(not isinstance(item, str) or not item.strip() for item in pending)
            or len(set(pending)) != len(pending)
        ):
            raise ValueError("待收集 ID 需为不重复的非空字符串元组，或未知。")

    def as_record(self) -> dict[str, Any]:
        """复制全部字段供后续有界展示；此版本尚不持久化或自动注入模型输入。"""
        return {
            "format": RUNTIME_STATE_VERSION,
            "requests": self.requests.as_record(),
            "tools": self.tools.as_record(),
            "tokens": self.tokens.as_record(),
            "failures": self.failures.as_record(),
            "environment": {"workspace": self.workspace, "mode": self.mode.value},
            "pending_agents": list(self.pending_agents)
            if self.pending_agents is not None
            else None,
        }

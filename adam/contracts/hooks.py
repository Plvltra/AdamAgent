"""本地 Hook 的版本化输入与严格响应；检查结果不授予工具权限。"""

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from adam.contracts.execution import ExecutionMode


class HookEvent(StrEnum):
    """工具前置决策与后置观察使用不同响应契约。"""

    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"


@dataclass(frozen=True)
class HookRequest:
    """一次工具边界的快照；source 来自执行器，after_tool 必须携带真实结果。"""

    event: HookEvent
    tool_name: str
    arguments: dict[str, Any]
    source: dict[str, str]
    mode: ExecutionMode
    result: dict[str, Any] | None = None

    def encode(self, workspace: str, limit: int) -> bytes:
        """生成实际工作目录下的输入；limit 为字节上限，非法来源或超限抛 ValueError，不截断输入。"""
        if (
            not isinstance(self.event, HookEvent)
            or not isinstance(self.mode, ExecutionMode)
            or not isinstance(self.tool_name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", self.tool_name)
            or not isinstance(self.arguments, dict)
            or not isinstance(self.source, dict)
            or self.source.keys() != {"session_id", "turn_id", "request_id", "tool_call_id"}
            or any(
                not isinstance(value, str) or not value or "\0" in value or len(value) > 256
                for value in self.source.values()
            )
            or (self.event is HookEvent.BEFORE_TOOL and self.result is not None)
            or (self.event is HookEvent.AFTER_TOOL and not isinstance(self.result, dict))
        ):
            raise ValueError("invalid hook request")
        payload = {
            "version": 1,
            "event": self.event.value,
            "workspace": workspace,
            "execution_mode": self.mode.value,
            "source": self.source,
            "tool": {"name": self.tool_name, "arguments": self.arguments},
        }
        if self.result is not None:
            payload["result"] = self.result
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ValueError("invalid hook JSON input") from error
        if len(encoded) > limit:
            raise ValueError("hook input exceeds limit")
        return encoded


@dataclass(frozen=True)
class HookResult:
    """检查器结果；failed 与 denied 区分，after_tool 的 observed 不能改写工具事实。"""

    status: Literal["allowed", "denied", "observed", "failed"]
    message: str = ""
    error_code: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    stderr: str = ""

    def to_record(self) -> dict[str, Any]:
        """返回可独立存储的结果副本，不包含程序环境或模型认证配置。"""
        return asdict(self)


class HookService(Protocol):
    """工具边界只依赖检查接口；实现不得获得模型客户端或自行派发工具。"""

    def configured(self, event: HookEvent) -> bool:
        """事件关闭时不创建进程或审计开始记录。"""
        ...

    def run(self, request: HookRequest) -> HookResult | None:
        """返回检查结果；取消与日志之外的程序故障继续传播。"""
        ...


def parse_reply(
    event: HookEvent, raw: bytes
) -> tuple[Literal["allowed", "denied", "observed"], str]:
    """校验单个完整响应；拒绝重复键、未知字段及非有限数值，不修补非协议文本。"""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate hook field")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError("non-finite hook value")

    data = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant
    )
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("invalid hook version")
    status: Literal["allowed", "denied", "observed"]
    if event is HookEvent.BEFORE_TOOL:
        if data.keys() - {"version", "decision", "reason"} or data.get("decision") not in (
            "allow",
            "deny",
        ):
            raise ValueError("invalid before hook response")
        text = data.get("reason", "")
        status = "allowed" if data["decision"] == "allow" else "denied"
    elif event is HookEvent.AFTER_TOOL:
        if data.keys() != {"version", "note"}:
            raise ValueError("invalid after hook response")
        text, status = data["note"], "observed"
    else:
        raise ValueError("invalid hook event")
    if not isinstance(text, str) or len(text) > 2000 or "\0" in text:
        raise ValueError("invalid hook message")
    text.encode("utf-8")  # JSON 中转义的孤立代理字符也不能进入后续日志。
    return status, text

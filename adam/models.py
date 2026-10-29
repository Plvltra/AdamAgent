"""模块间传递的业务数据；不包含文件、HTTP 或进程操作。"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class Session:
    """会话身份及固定系统提示词。"""

    session_id: str
    title: str
    system_prompt: str


@dataclass(frozen=True)
class Message:
    """已提交的会话消息；raw 保留工具调用和服务商推理字段。"""

    message_id: str
    session_id: str
    position: int
    role: str
    content: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    """一次有效模型响应；usage 为输入/输出 token，缺失时为 None。"""

    message: dict[str, Any]
    usage: tuple[int, int] | None


@dataclass(frozen=True)
class CommandRequest:
    """用户确认的不可变执行计划，argv 使用参数列表，不经过 shell。"""

    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: int


@dataclass(frozen=True)
class SessionUsage:
    """会话已知累计用量、消费上限及统计是否完整。"""

    tokens: int
    limit: int
    unknown: bool


@dataclass(frozen=True)
class ContinuationStatus:
    """当前会话是否允许继续消费及原因；不代表已发生的外部副作用可以回滚。"""

    allowed: bool
    reason: str
    message: str


ExecutionRecorder = Callable[[str, dict[str, Any]], None]


def valid_session_id(value: str) -> bool:
    """判断输入是否为完整的 8 位小写十六进制会话 ID。"""
    return re.fullmatch(r"[0-9a-f]{8}", value) is not None


@dataclass(frozen=True)
class EditPlan:
    """批准对象：确定的路径、原版本和候选字节；diff 只用于展示。"""

    path: Path
    before_version: str
    candidate: bytes
    diff: str


@dataclass(frozen=True)
class ModelOutput:
    """模型展示事件；response_end 只表示输出边界，不表示轮次已保存。"""

    kind: Literal["text", "response_end"]
    text: str = ""

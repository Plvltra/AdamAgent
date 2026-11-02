"""模块间传递的业务数据；不包含文件、HTTP 或进程操作。"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from adam.contracts.command_execution import CommandInputPreview
from adam.contracts.usage import CacheUsage, InputPrefix


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
    """一次有效响应；usage 为输入/输出 token，缓存和前缀独立记录；缺失保持 None。"""

    message: dict[str, Any]
    usage: tuple[int, int] | None
    cache_usage: CacheUsage | None = None
    input_prefix: InputPrefix | None = None


@dataclass(frozen=True)
class CommandRequest:
    """用户确认的不可变执行计划；argv 不拼接 shell，environment 明确区分宿主与隔离。

    隔离 cwd 固定为 /work，input_files 仅供展示已授权快照来源和版本，不授予路径权限。
    """

    argv: tuple[str, ...]
    cwd: Path | PurePosixPath
    timeout_seconds: int
    environment: Literal["host", "isolated"] = "host"
    distribution: str | None = None
    input_files: tuple[CommandInputPreview, ...] = ()


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
    """批准对象：路径、原版本及前后字节；original 用于检查点，diff 只用于展示。"""

    path: Path
    before_version: str
    candidate: bytes
    diff: str
    original: bytes


@dataclass(frozen=True)
class ModelOutput:
    """模型展示事件；response_end 只表示输出边界，不表示轮次已保存。"""

    kind: Literal["text", "response_end"]
    text: str = ""


@dataclass(frozen=True)
class ActionFact:
    """一次实际派发的副作用工具；目标已用 JSON 转义，避免换行或控制符伪造报告。"""

    tool_call_id: str
    kind: str
    target: str
    status: str
    exit_code: int | None = None
    before_version: str | None = None
    after_version: str | None = None


@dataclass(frozen=True)
class ExecutionReport:
    """轮次执行快照；成功状态只表示回答提交，不承诺任务或测试正确。"""

    turn_id: str
    status: str
    reason: str | None
    has_tools: bool
    actions: tuple[ActionFact, ...]

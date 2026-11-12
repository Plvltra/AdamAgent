"""请求和轮次的结束信息；分类与校验不依赖存储或网络模块。"""

from dataclasses import dataclass
from typing import Any, Literal

from adam.errors import AgentError, EndReason

EndStatus = Literal["succeeded", "failed", "interrupted"]


@dataclass(frozen=True)
class ExecutionOutcome:
    """可落盘的结束信息；duration_ms 为毫秒，未测量时只能用 None。"""

    status: EndStatus
    reason: EndReason
    duration_ms: int | None = None
    error_message: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        """拒绝非法类型与互相矛盾的状态，避免记录看起来完整却含义错误。"""
        if self.status not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("invalid outcome status")
        if not isinstance(self.reason, EndReason):
            raise ValueError("invalid outcome reason")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or self.duration_ms < 0
        ):
            raise ValueError("invalid duration")
        interrupted = self.reason in {EndReason.INTERRUPTED, EndReason.PROCESS_EXIT}
        if (self.status == "succeeded") != (self.reason == EndReason.COMPLETED):
            raise ValueError("inconsistent success reason")
        if (self.status == "interrupted") != interrupted:
            raise ValueError("inconsistent interruption reason")
        if self.status == "succeeded":
            if self.error_message is not None:
                raise ValueError("successful outcome contains error")
        elif not isinstance(self.error_message, str) or not self.error_message.strip():
            raise ValueError("missing controlled error message")
        if self.reason == EndReason.API_HTTP_ERROR:
            if (
                type(self.http_status) is not int
                or not 100 <= self.http_status <= 599
                or 200 <= self.http_status < 300
            ):
                raise ValueError("invalid HTTP error status")
        elif self.http_status is not None:
            raise ValueError("HTTP status without HTTP failure")

    @classmethod
    def success(cls, duration_ms: int | None = None) -> "ExecutionOutcome":
        """构造正常结束信息；直接存储消息时可明确保留未知耗时。"""
        return cls("succeeded", EndReason.COMPLETED, duration_ms)

    @classmethod
    def failure(cls, error: BaseException, duration_ms: int | None) -> "ExecutionOutcome":
        """按异常类型分类；不保存意外异常文本、原始响应正文或 traceback。"""
        if isinstance(error, AgentError):
            return cls("failed", error.reason, duration_ms, error.user_message, error.http_status)
        if isinstance(error, SystemExit):
            return cls("interrupted", EndReason.PROCESS_EXIT, duration_ms, "程序退出。")
        if not isinstance(error, Exception):
            return cls("interrupted", EndReason.INTERRUPTED, duration_ms, "执行已中断。")
        return cls("failed", EndReason.INTERNAL_ERROR, duration_ms, "程序内部错误，本轮停止。")

    @classmethod
    def from_payload(cls, payload: dict[str, Any], status: EndStatus) -> "ExecutionOutcome":
        """严格读取 v5 必填字段；缺失不能按旧格式或默认值解释。"""
        fields = {"reason", "duration_ms", "error_message", "http_status"}
        if not fields <= payload.keys():
            raise ValueError("missing outcome details")
        return cls(
            status,
            EndReason(payload["reason"]),
            payload["duration_ms"],
            payload["error_message"],
            payload["http_status"],
        )

    def details(self) -> dict[str, Any]:
        """返回结束字段；状态仍由原有结束事件的契约表达。"""
        return {
            "reason": self.reason.value,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class RequestResult:
    """一次模型请求的用量和结束结果，与整轮结果独立。

    usage 为 (输入 token, 输出 token)；None 表示未知而非零。
    outcome 必须显式提供；请求成功仍可能没有用量，失败也可能已有用量。
    """

    usage: tuple[int, int] | None
    outcome: ExecutionOutcome


@dataclass(frozen=True)
class TurnCommit:
    """一轮待提交的完整消息及结束结果；消息与成功标记必须一起保存。

    messages 包含用户输入、工具轨迹与最终回答；存储入口校验完整性与成功状态。
    对象不复制消息，提交期间调用方不得修改消息内容。
    """

    messages: list[dict[str, Any]]
    outcome: ExecutionOutcome

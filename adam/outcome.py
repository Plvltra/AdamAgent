"""请求与轮次共用的结束信息；只保存受控原因、错误说明和已测得的耗时。"""

from dataclasses import dataclass
from typing import Any, Literal, Self

from adam.errors import AgentError, FailureCode

TerminalStatus = Literal["succeeded", "failed", "interrupted"]


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次操作的终态及证据；duration_ms 为单调时钟测得的毫秒，None 表示未知。

    成功只能使用 completed 且没有错误；失败说明不采用原始异常堆栈或服务端正文。
    本对象只表达结果，是否已成功落盘由存储层决定。
    """

    status: TerminalStatus
    reason: str
    duration_ms: int | None = None
    message: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        """写入与回放共用校验，拒绝非法状态组合、负耗时和伪装成整数的布尔值。"""
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or self.duration_ms < 0
        ):
            raise ValueError("invalid duration")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid HTTP status")
        if self.status == "succeeded":
            if (
                self.reason != "completed"
                or self.message is not None
                or self.http_status is not None
            ):
                raise ValueError("invalid success details")
            return
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("missing failure message")
        code = FailureCode(self.reason)
        interrupted = code in {
            FailureCode.INTERRUPTED,
            FailureCode.PROCESS_EXIT,
            FailureCode.RESTART_RECOVERY,
        }
        expected = "interrupted" if interrupted else "failed"
        if self.status != expected:
            raise ValueError("reason does not match status")
        if self.http_status is not None and code != FailureCode.API_HTTP:
            raise ValueError("HTTP status requires HTTP failure")

    @classmethod
    def success(cls, duration_ms: int | None = None) -> Self:
        """表达正常结束；不测量或无法可靠测量的耗时保持未知。"""
        return cls("succeeded", "completed", duration_ms)

    @classmethod
    def failure(cls, error: BaseException, duration_ms: int | None = None) -> Self:
        """从公共错误契约生成失败说明；未知异常只保存通用原因，不复制异常内容。"""
        if isinstance(error, AgentError):
            return cls(
                "failed", error.code.value, duration_ms, error.user_message, error.http_status
            )
        if not isinstance(error, Exception):
            code = (
                FailureCode.PROCESS_EXIT
                if isinstance(error, SystemExit)
                else FailureCode.INTERRUPTED
            )
            return cls(
                "interrupted",
                code.value,
                duration_ms,
                "程序退出。" if isinstance(error, SystemExit) else "用户中断本轮。",
            )
        return cls(
            "failed",
            FailureCode.INTERNAL_ERROR.value,
            duration_ms,
            "程序内部错误；请检查本地异常堆栈。",
        )

    def payload(self) -> dict[str, Any]:
        """返回可直接合入终态事件的字段；成功时 error 为 null。"""
        error = (
            None
            if self.message is None
            else {"message": self.message, "http_status": self.http_status}
        )
        return {
            "status": self.status,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "error": error,
        }


def outcome_details(payload: dict[str, Any], status: TerminalStatus) -> dict[str, Any]:
    """校验终态扩展字段；早期 v4 缺少全部字段时保留未知，不猜原因或耗时。"""
    fields = {"reason", "duration_ms", "error"}
    if not fields.intersection(payload):
        return {"reason": None, "duration_ms": None, "error": None}
    if not fields.issubset(payload):
        raise ValueError("incomplete outcome details")
    error = payload["error"]
    if error is not None and (
        not isinstance(error, dict)
        or set(error) != {"message", "http_status"}
        or not isinstance(error["message"], str)
    ):
        raise ValueError("invalid error details")
    outcome = ExecutionOutcome(
        status,
        payload["reason"],
        payload["duration_ms"],
        error["message"] if error is not None else None,
        error["http_status"] if error is not None else None,
    )
    return outcome.payload()

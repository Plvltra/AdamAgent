"""界面与业务模块共享的失败契约；不依赖网络、存储或工具实现。"""

from enum import StrEnum


class EndReason(StrEnum):
    """JSONL 的稳定结束原因；与可修改措辞的用户提示分开保存。"""

    COMPLETED = "completed"
    API_TIMEOUT = "api_timeout"
    API_CONNECTION = "api_connection"
    API_HTTP_ERROR = "api_http_error"
    INVALID_RESPONSE = "invalid_response"
    USAGE_UNKNOWN = "usage_unknown"
    REQUEST_LIMIT = "request_limit"
    TOOL_LIMIT = "tool_limit"
    SESSION_LIMIT = "session_limit"
    BUDGET_LIMIT = "budget_limit"
    INTERRUPTED = "interrupted"
    PROCESS_EXIT = "process_exit"
    STORAGE_ERROR = "storage_error"
    TOOL_ERROR = "tool_error"
    COMMAND_LOG_ERROR = "command_log_error"
    ANSWER_SAVE_FAILED = "answer_save_failed"
    BUSINESS_ERROR = "business_error"
    INTERNAL_ERROR = "internal_error"


class AgentError(Exception):
    """可向用户展示的已知失败；不负责恢复或重试。

    user_message 必须是受控的业务说明，不直接采用原始系统异常或远端响应正文。
    user_notes 只放可公开的补充说明，不能直接复制底层异常附注。
    partial_reply 仅用于已生成但未保存的回答；界面无需根据底层异常类型选择展示方式。
    """

    reason = EndReason.BUSINESS_ERROR

    def __init__(
        self,
        user_message: str,
        *,
        partial_reply: str | None = None,
        reason: EndReason | None = None,
        http_status: int | None = None,
    ) -> None:
        """保留受控提示及结构化原因；http_status 仅供 HTTP 失败记录。"""
        super().__init__(user_message)
        self.user_message = user_message
        self.user_notes: list[str] = []
        self.partial_reply = partial_reply
        self.reason = reason if reason is not None else type(self).reason
        self.http_status = http_status


class BudgetError(AgentError):
    """额度耗尽或记账不完整导致停止，与模型通信失败分别表达。"""

    reason = EndReason.BUDGET_LIMIT

    def __init__(self, message: str, *, reason: EndReason | None = None) -> None:
        super().__init__(f"执行受限：{message}", reason=reason)


class TurnSaveError(AgentError):
    """模型已回答但本轮未能保存；保留回答供界面展示，不重新请求模型。"""

    reason = EndReason.ANSWER_SAVE_FAILED

    def __init__(self, reply: str) -> None:
        super().__init__("本轮保存失败，未加入历史；未自动重新请求模型。", partial_reply=reply)

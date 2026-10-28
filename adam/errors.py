"""界面与业务模块共享的失败契约；不依赖网络、存储或工具实现。"""

from enum import StrEnum


class FailureCode(StrEnum):
    """持久化使用的稳定失败类别；不依赖异常提示文字进行分类。"""

    AGENT_ERROR = "agent_error"
    API_ERROR = "api_error"
    API_TIMEOUT = "api_timeout"
    API_CONNECTION = "api_connection"
    API_HTTP = "api_http"
    INVALID_RESPONSE = "invalid_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REQUEST_LIMIT = "request_limit"
    TOOL_LIMIT = "tool_limit"
    SESSION_TOKEN_LIMIT = "session_token_limit"
    USAGE_UNKNOWN = "usage_unknown"
    ANSWER_SAVE_FAILED = "answer_save_failed"
    REQUEST_SAVE_FAILED = "request_save_failed"
    STORAGE_ERROR = "storage_error"
    COMMAND_LOG_ERROR = "command_log_error"
    EXECUTION_UNKNOWN = "execution_unknown"
    INTERNAL_ERROR = "internal_error"
    INTERRUPTED = "interrupted"
    PROCESS_EXIT = "process_exit"


class AgentError(Exception):
    """可向用户展示的已知失败；不负责恢复或重试。

    user_message 必须是受控的业务说明，不直接采用原始系统异常或远端响应正文。
    code 提供日志分类，http_status 只携带 HTTP 数值，不保存远端正文。
    partial_reply 仅用于已生成但未保存的回答；界面无需根据底层异常类型选择展示方式。
    """

    code: FailureCode = FailureCode.AGENT_ERROR

    def __init__(
        self,
        user_message: str,
        *,
        partial_reply: str | None = None,
        code: FailureCode | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.partial_reply = partial_reply
        self.code = code if code is not None else self.code
        self.http_status = http_status


class BudgetError(AgentError):
    """额度耗尽或记账不完整导致停止，与模型通信失败分别表达。"""

    code = FailureCode.BUDGET_EXHAUSTED

    def __init__(self, message: str, *, code: FailureCode | None = None) -> None:
        super().__init__(f"执行受限：{message}", code=code)


class TurnSaveError(AgentError):
    """模型已回答但本轮未能保存；保留回答供界面展示，不重新请求模型。"""

    code = FailureCode.ANSWER_SAVE_FAILED

    def __init__(self, reply: str, *, code: FailureCode | None = None) -> None:
        super().__init__(
            "本轮保存失败，未加入历史；未自动重新请求模型。", partial_reply=reply, code=code
        )

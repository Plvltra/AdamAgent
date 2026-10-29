"""界面与业务模块共享的失败契约；不依赖网络、存储或工具实现。"""

from enum import StrEnum


class FailureCode(StrEnum):
    """持久化使用的稳定失败类别；不依赖异常提示文字进行分类。"""

    AGENT_ERROR = "agent_error"
    API_ERROR = "api_error"
    API_TIMEOUT = "api_timeout"
    API_CONNECTION = "api_connection"
    API_NOT_SENT = "api_not_sent"
    API_HTTP = "api_http"
    INVALID_RESPONSE = "invalid_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REQUEST_LIMIT = "request_limit"
    TOOL_LIMIT = "tool_limit"
    EDIT_LIMIT = "edit_limit"
    SESSION_TOKEN_LIMIT = "session_token_limit"
    CONTEXT_INPUT_LIMIT = "context_input_limit"
    USAGE_UNKNOWN = "usage_unknown"
    ANSWER_SAVE_FAILED = "answer_save_failed"
    REQUEST_SAVE_FAILED = "request_save_failed"
    STORAGE_ERROR = "storage_error"
    COMMAND_LOG_ERROR = "command_log_error"
    EDIT_LOG_ERROR = "edit_log_error"
    APPROVAL_DENIED = "approval_denied"
    EXECUTION_UNKNOWN = "execution_unknown"
    INTERNAL_ERROR = "internal_error"
    INTERRUPTED = "interrupted"
    PROCESS_EXIT = "process_exit"
    RESTART_RECOVERY = "restart_recovery"


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


class EditLogError(AgentError):
    """编辑记录失败后停止，不能将未知副作用当作可重试参数错误。"""

    code = FailureCode.EDIT_LOG_ERROR


class ApprovalDeniedError(AgentError):
    """用户拒绝当前动作，终止本轮剩余工具与模型请求。"""

    code = FailureCode.APPROVAL_DENIED


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


class ConfigError(AgentError):
    """配置无法使用；提示只包含受控字段信息。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"配置错误：{message}")


class LLMError(AgentError):
    """模型通信或响应失败；已取得的用量仍由会话流程结算。"""

    code = FailureCode.API_ERROR

    def __init__(
        self,
        message: str,
        usage: tuple[int, int] | None = None,
        *,
        code: FailureCode | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"模型请求错误：{message}", code=code, http_status=http_status)
        self.usage = usage


class RequestNotSentError(LLMError):
    """连接建立失败，HTTP 请求尚未发送；只有此类失败允许策略层有限重试。"""

    def __init__(self) -> None:
        super().__init__("连接未建立，请求未发出。", (0, 0), code=FailureCode.API_NOT_SENT)


class StorageError(AgentError):
    """会话占用、格式损坏或写入失败。"""

    code = FailureCode.STORAGE_ERROR


class MemoryStoreError(AgentError):
    """记忆读取或写入失败；不等同于会话日志失败。"""


class MemoryInputError(AgentError):
    """记忆参数无效、目标不存在或版本已变化；调用没有完成修改。"""


class SessionNotFoundError(AgentError):
    """指定会话不存在。"""


class ToolError(AgentError):
    """可以安全回传给模型的工具参数或权限错误。"""


class CommandLogError(AgentError):
    """执行日志写入失败；停止本轮，避免把未知执行状态当作可重试错误。"""

    code = FailureCode.COMMAND_LOG_ERROR


class RequestInterrupted(KeyboardInterrupt):
    """请求中断；保留在中断前已收到的可靠用量，不把取消当模型失败。"""

    def __init__(self, usage: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.usage = usage

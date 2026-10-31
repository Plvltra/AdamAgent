"""界面与业务模块共享的失败契约；不依赖网络、存储或工具实现。"""

from enum import StrEnum

from adam.contracts.usage import CacheUsage, InputPrefix


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
    NO_PROGRESS = "no_progress"
    EDIT_LIMIT = "edit_limit"
    SESSION_TOKEN_LIMIT = "session_token_limit"
    CONTEXT_INPUT_LIMIT = "context_input_limit"
    USAGE_UNKNOWN = "usage_unknown"
    ANSWER_SAVE_FAILED = "answer_save_failed"
    REQUEST_SAVE_FAILED = "request_save_failed"
    STORAGE_ERROR = "storage_error"
    COMMAND_LOG_ERROR = "command_log_error"
    EDIT_LOG_ERROR = "edit_log_error"
    HOOK_LOG_ERROR = "hook_log_error"
    TOOL_CLEANUP_ERROR = "tool_cleanup_error"
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


class HookLogError(AgentError):
    """Hook 审计失败后停止，不把未记录的决策当作可重试工具错误。"""

    code = FailureCode.HOOK_LOG_ERROR


class ToolCleanupError(AgentError):
    """清理期限内仍有读取线程未退出；本调度器之后拒绝再运行任务。"""

    code = FailureCode.TOOL_CLEANUP_ERROR

    def __init__(self, call_ids: tuple[str, ...]) -> None:
        self.call_ids = call_ids
        super().__init__("只读工具线程未能及时退出，已禁止继续派发工具；请结束本次进程后再运行。")


class ApprovalDeniedError(AgentError):
    """用户拒绝当前动作，终止本轮剩余工具与模型请求。"""

    code = FailureCode.APPROVAL_DENIED


class NoProgressError(AgentError):
    """重复失败达到本轮上限；执行证据已按原边界保存，不隐式重试。"""

    code = FailureCode.NO_PROGRESS

    def __init__(self, repeats: int) -> None:
        super().__init__(f"相同工具调用已连续失败 {repeats} 次，本轮已停止；请检查原因后调整任务。")


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
    """模型通信或响应失败；已取得的用量与观测仍交给会话流程结算和保存。"""

    code = FailureCode.API_ERROR

    def __init__(
        self,
        message: str,
        usage: tuple[int, int] | None = None,
        *,
        code: FailureCode | None = None,
        http_status: int | None = None,
        cache_usage: CacheUsage | None = None,
        input_prefix: InputPrefix | None = None,
    ) -> None:
        super().__init__(f"模型请求错误：{message}", code=code, http_status=http_status)
        self.usage = usage
        self.cache_usage = cache_usage
        self.input_prefix = input_prefix


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


class ToolHookError(ToolError):
    """前置检查阻止本次动作；不代表用户拒绝整个轮次，也不能触发自动反思。"""


class ToolModeError(ToolError):
    """当前模式禁止执行；这是策略边界，不是用户拒绝或待自动修复的工具故障。"""


class ToolDeferredError(ToolError):
    """需要先补充模型上下文，工具尚未进入实际操作；不作为自动反思的失败线索。"""


class CommandLogError(AgentError):
    """执行日志写入失败；停止本轮，避免把未知执行状态当作可重试错误。"""

    code = FailureCode.COMMAND_LOG_ERROR


class RequestInterrupted(KeyboardInterrupt):
    """请求中断；保留在中断前已收到的可靠用量，不把取消当模型失败。"""

    def __init__(
        self,
        usage: tuple[int, int] | None = None,
        *,
        cache_usage: CacheUsage | None = None,
        input_prefix: InputPrefix | None = None,
    ) -> None:
        super().__init__()
        self.usage = usage
        self.cache_usage = cache_usage
        self.input_prefix = input_prefix

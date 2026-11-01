"""浏览器逐次批准的不可变展示数据；不携带 SDK、cookies 或执行回调。"""

from dataclasses import dataclass

from adam.contracts.errors import AgentError, FailureCode


@dataclass(frozen=True)
class BrowserApproval:
    """宿主确认的确切目标和输入；页面标签不可信，界面须转义后展示。"""

    operation: str
    url: str
    name: str = ""
    reference: tuple[str, str, str, str] | None = None
    value: str | None = None
    scroll_y: int | None = None
    browser_id: str | None = None
    tab_id: str | None = None


class BrowserActionUnknownError(AgentError):
    """动作已进入 SDK 而结果不确定；停止轮次，不能作为可重试参数错误。"""

    code = FailureCode.EXECUTION_UNKNOWN

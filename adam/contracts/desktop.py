"""桌面逐次批准只描述确切目标；不携带 SDK 或执行回调。"""

from dataclasses import dataclass

from adam.contracts.errors import AgentError, FailureCode


@dataclass(frozen=True)
class DesktopApproval:
    """窗口文字不可信；宿主应转义展示，批准不跨动作或观察复用。"""

    handle: int
    process_id: int
    version: str
    operation: str
    control: int | None = None
    value: str | None = None
    point: tuple[int, int] | None = None


class DesktopActionUnknownError(AgentError):
    """动作已进入原生调用但无法确定结果，停止轮次且不得自动重试。"""

    code = FailureCode.EXECUTION_UNKNOWN

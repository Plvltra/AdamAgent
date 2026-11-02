"""Android逐次批准契约；页面文字不赋予授权。"""

from dataclasses import dataclass

from adam.contracts.errors import AgentError, FailureCode


@dataclass(frozen=True)
class MobileApproval:
    """固定模拟器、观察版本及一次ADB输入参数；批准不可跨动作复用。"""

    serial: str
    version: str
    operation: str
    arguments: tuple[str, ...]


class MobileActionUnknownError(AgentError):
    """已进入ADB输入但结果不确定；停止原轮次，不自动重试。"""

    code = FailureCode.EXECUTION_UNKNOWN

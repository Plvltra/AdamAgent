"""界面与业务模块共享的失败契约；不依赖网络、存储或工具实现。"""


class AgentError(Exception):
    """可向用户展示的已知失败；不负责恢复或重试。

    user_message 必须是受控的业务说明，不直接采用原始系统异常或远端响应正文。
    partial_reply 仅用于已生成但未保存的回答；界面无需根据底层异常类型选择展示方式。
    """

    def __init__(self, user_message: str, *, partial_reply: str | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.partial_reply = partial_reply


class BudgetError(AgentError):
    """额度耗尽或记账不完整导致停止，与模型通信失败分别表达。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"执行受限：{message}")


class TurnSaveError(AgentError):
    """模型已回答但本轮未能保存；保留回答供界面展示，不重新请求模型。"""

    def __init__(self, reply: str) -> None:
        super().__init__("本轮保存失败，未加入历史；未自动重新请求模型。", partial_reply=reply)

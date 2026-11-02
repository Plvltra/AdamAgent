"""最终回答前的独立验收与有界修正，不拥有额外模型或工具额度。"""

from dataclasses import asdict

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError, FailureCode
from adam.contracts.verification import TaskVerifier
from adam.session.storage import SessionStore


class CompletionVerifier:
    """每轮独立实例；最多反馈两次修正，未知验收立即停止，不保存虚假成功回答。"""

    def __init__(
        self,
        verifier: TaskVerifier,
        store: SessionStore,
        session_id: str,
        turn_id: str,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.verifier, self.store = verifier, store
        self.session_id, self.turn_id, self.cancel = session_id, turn_id, cancel
        self.attempts = 0
        self.repairs = 0

    def review(self, request_id: str, reply: str) -> str | None:
        """先验实际终态并落盘再允许收尾/修正；存储或取消失败沿用原停止路径。"""
        if self.cancel:
            self.cancel.check()
        result = self.verifier.check(reply)
        if self.cancel:
            self.cancel.check()
        self.attempts += 1
        self.store.append_verification(
            self.session_id,
            {
                "turn_id": self.turn_id,
                "request_id": request_id,
                "attempt": self.attempts,
                **asdict(result),
            },
        )
        if result.status == "passed":
            return None
        if result.status == "unknown":
            raise AgentError(
                "无法完成独立验收，本轮停止，未确认任务成功。",
                code=FailureCode.VERIFICATION_UNKNOWN,
            )
        if self.repairs >= 2:
            raise AgentError(
                "两轮修正后仍未通过独立验收，本轮停止。", code=FailureCode.VERIFICATION_FAILED
            )
        self.repairs += 1
        return "宿主独立验收未通过，请按原任务要求修正后重新回答；不新增权限或额度。\n" + "\n".join(
            result.diagnostics
        )

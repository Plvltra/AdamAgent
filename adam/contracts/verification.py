"""宿主选择的独立终态验收契约；不接受模型定义判据。"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class VerificationResult:
    """通过、明确失败与无法验证分开；diagnostics供修正参考，版本用于日志追溯。"""

    status: Literal["passed", "failed", "unknown"]
    verifier_id: str
    version: str
    diagnostics: tuple[str, ...]
    observed_files: tuple[tuple[str, str | None], ...] = ()


class TaskVerifier(Protocol):
    """由可信宿主注入的只读验收器；模型正文只是候选回答，不能覆盖标准。"""

    def check(self, reply: str) -> VerificationResult:
        """检查当前实际终态；无法取得证据时返回unknown，不用模型自述补齐。"""
        ...

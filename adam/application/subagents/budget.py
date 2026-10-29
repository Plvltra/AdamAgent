"""并发请求共享额度；在途预留与已经无法确定的消费分别记录。"""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from adam.contracts.errors import BudgetError, FailureCode


class SharedTokenBudget:
    """按会话保存累计用量快照；调用者在请求结算后更新，不重复累计历史。

    预留值是输入估算加输出上限，用于防止并发请求重复使用同一份剩余额度。
    模型分词与实际消费可能不同，因此这不是服务端费用的严格硬上限。
    已在途的请求可以完成结算；未知消费阻止新请求，不主动取消其他请求。
    """

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._totals: dict[str, tuple[int, bool]] = {}
        self._reserved = 0
        self._lock = RLock()

    def update(self, owner: str, tokens: int, unknown: bool) -> None:
        """替换一个成员的持久化累计快照；仅由该成员拥有日志的线程调用。"""
        if tokens < 0:
            raise ValueError("negative token usage")
        with self._lock:
            self._totals[owner] = (tokens, unknown)

    def usage(self) -> tuple[int, bool]:
        """返回整组已知消费及未知标记；正常在途预留不算未知消费。"""
        with self._lock:
            return (
                sum(tokens for tokens, _ in self._totals.values()),
                any(unknown for _, unknown in self._totals.values()),
            )

    def check(self) -> None:
        """额度耗尽或曾有未知消费时拒绝新增请求，保留所有已知消费。"""
        with self._lock:
            tokens, unknown = self.usage()
            if unknown:
                raise BudgetError(
                    "主会话或子 Agent 用量未知；已停止新增请求，不自动重发。",
                    code=FailureCode.USAGE_UNKNOWN,
                )
            if tokens >= self.maximum:
                raise BudgetError("主会话共享额度已用完。", code=FailureCode.SESSION_TOKEN_LIMIT)

    @contextmanager
    def reserve(self, estimate: int) -> Iterator[None]:
        """在发请求前原子预留，退出时释放；调用方须在退出前更新实际消费快照。"""
        if estimate <= 0:
            raise ValueError("reservation must be positive")
        with self._lock:
            self.check()
            tokens, _ = self.usage()
            if tokens + self._reserved + estimate > self.maximum:
                raise BudgetError(
                    "剩余额度不足以预留本次请求；可等待在途请求结束或减少输入。",
                    code=FailureCode.SESSION_TOKEN_LIMIT,
                )
            self._reserved += estimate
        try:
            yield
        finally:
            with self._lock:
                self._reserved -= estimate

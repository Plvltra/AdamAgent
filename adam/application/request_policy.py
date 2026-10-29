"""单轮物理请求额度与有限连接重试；不重放工具，不处理已发出的失败请求。"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from adam.contracts.errors import BudgetError, FailureCode, RequestNotSentError

_Result = TypeVar("_Result")


@dataclass
class RequestPolicy:
    """一次用户操作共用的额度；每个实际尝试占一次，最多额外重试两次。"""

    maximum: int
    notify: Callable[[str], None]
    used: int = 0

    def run(self, operation: Callable[[], _Result], *, keep_requests: int = 0) -> _Result:
        """调用带独立持久记录的请求；仅明确未发出的连接错误允许重试。

        keep_requests 为后续正式回答保留次数。次数或日志失败直接传播；
        短退避可被 Ctrl+C 中断，不以后台线程继续请求。
        """
        for attempt in range(3):
            if self.used >= self.maximum - keep_requests:
                raise BudgetError("达到本轮物理请求上限。", code=FailureCode.REQUEST_LIMIT)
            self.used += 1
            try:
                return operation()
            except RequestNotSentError:
                if attempt == 2:
                    raise
                if self.used >= self.maximum - keep_requests:
                    raise BudgetError(
                        "剩余请求额度不足以重试。", code=FailureCode.REQUEST_LIMIT
                    ) from None
                self.notify(f"连接未建立，请求未发出；准备第 {attempt + 1}/2 次重试。")
                time.sleep(0.2 * (attempt + 1))
        raise AssertionError("unreachable")

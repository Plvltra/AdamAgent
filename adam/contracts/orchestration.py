"""隔离编排的窄接口；工具层不依赖具体 OS 后端或宿主进程实现。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from adam.contracts.cancellation import CancellationToken

ToolCallback = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class OrchestrationResult:
    """最终结果和实际调用数；has_result 为假时不能把空值宣称成脚本的成功结果。"""

    result: Any
    has_result: bool
    exit_code: int
    reason: str
    calls: int
    stderr: str


class CodeRunner(Protocol):
    """宿主固定后端执行有界代码，只能经回调取得工具结果，不暴露注册表对象。"""

    def run(
        self, code: str, invoke: ToolCallback, *, cancel: CancellationToken | None = None
    ) -> OrchestrationResult:
        """在调用线程执行回调；取消、隔离和回调故障向上传播，不自动重试。"""
        ...

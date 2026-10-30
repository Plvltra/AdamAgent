"""用户控制的本次运行模式；任务期间固定，不能被模型或并发界面回调切换。"""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from adam.contracts.errors import AgentError
from adam.contracts.execution import ExecutionMode


class ModeController:
    """由应用持有切换入口；工具和子任务调度只得到 current 读取器。"""

    def __init__(self) -> None:
        self._mode = ExecutionMode.ACT
        self._running = False
        self._lock = Lock()

    def current(self) -> ExecutionMode:
        """返回当前终端模式；查询不创建会话，也不改变运行状态。"""
        with self._lock:
            return self._mode

    def select(self, value: str) -> None:
        """仅接受 plan/act；非法值或当前任务尚未收尾时保留原模式。"""
        try:
            mode = ExecutionMode(value)
        except (TypeError, ValueError) as error:
            raise AgentError("用法：/mode [plan|act]；模式未改变。") from error
        with self._lock:
            if self._running:
                raise AgentError("当前任务尚未结束，不能切换模式；请等待完成或先取消。")
            self._mode = mode

    @contextmanager
    def turn(self) -> Iterator[None]:
        """固定整轮模式直至子任务清理完毕；成功、失败或取消均解除占用。"""
        with self._lock:
            if self._running:
                raise AgentError("当前终端已有运行中的任务，不能同时提交另一轮。")
            self._running = True
        try:
            yield
        finally:
            with self._lock:
                self._running = False

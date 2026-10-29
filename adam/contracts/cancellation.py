"""跨线程停止信号；取消只影响持有同一令牌的任务，不清除会话记录。"""

from threading import Event

from adam.contracts.errors import RequestInterrupted


class CancellationToken:
    """协作取消：HTTP 等待与业务阶段边界检查信号，清理资源时不再次检查。"""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        """供异步 IO 适配器检查，不在事件循环内部抛出 KeyboardInterrupt。"""
        return self._event.is_set()

    def cancel(self) -> None:
        """请求停止；可重复调用，不在调用线程执行目标任务的清理。"""
        self._event.set()

    def check(self) -> None:
        """已取消时抛中断信号；实际请求已知用量由模型适配器补充。"""
        if self._event.is_set():
            raise RequestInterrupted()

    def wait(self, seconds: float) -> None:
        """等待短暂退避或任务进度，取消时立即返回中断。"""
        if self._event.wait(seconds):
            self.check()

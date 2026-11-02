"""区分网络存活与模型进展；由读取边界调用，不启动监控线程。"""

import time
from collections.abc import Callable, Iterable, Iterator


class StreamDeadlineError(TimeoutError):
    """到达语义空闲或总时限，传输层负责保留已知用量。"""


class StreamWatch:
    """使用单调时钟；网络字节和心跳不刷新有效输出时间。"""

    def __init__(
        self,
        idle_seconds: float,
        total_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.idle_seconds, self.total_seconds = idle_seconds, total_seconds
        self.started = self.progress_at = clock()
        self.network_bytes = 0

    def received(self, size: int) -> None:
        """检查期限并统计字节；心跳不能续期。"""
        self.check()
        self.network_bytes += size

    def chunks(self, source: Iterable[bytes]) -> Iterator[bytes]:
        """在SSE过滤心跳前检查每批网络字节，结束时也核对期限。"""
        for chunk in source:
            self.received(len(chunk))
            yield chunk
        self.check()

    def progressed(self) -> None:
        """有效正文、推理或工具增量后续期；迟到输出不能救活过期请求。"""
        self.check()
        self.progress_at = self.clock()

    def check(self) -> None:
        """到达期限抛StreamDeadlineError，不强制中断尚未返回的系统调用。"""
        now = self.clock()
        if now - self.started >= self.total_seconds:
            raise StreamDeadlineError("流式请求达到总时限。")
        if now - self.progress_at >= self.idle_seconds:
            raise StreamDeadlineError("流式请求长时间没有有效模型输出。")

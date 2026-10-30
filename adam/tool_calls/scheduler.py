"""同批工具调度；只有可信声明的无共享状态只读动作进入工作线程。"""

import math
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from itertools import groupby
from threading import Event, Lock, Thread
from typing import Generic, TypeVar

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolCleanupError
from adam.contracts.execution import ToolCapability

T = TypeVar("T")
ToolAction = Callable[[CancellationToken], T]


@dataclass(frozen=True)
class ToolTask(Generic[T]):
    """一个已关联调用 ID 的任务；prepare 必须在协调线程完成预检并返回动作。

    parallel_safe 只由注册代码声明：动作不得写会话、调用 UI 或修改共享状态，
    必须定期检查传入的取消令牌，并为阻塞 IO 设置有限等待。只读能力本身不够。
    """

    call_id: str
    prepare: Callable[[], ToolAction[T]]
    capability: ToolCapability = ToolCapability.UNKNOWN
    parallel_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id or not callable(self.prepare):
            raise ValueError("工具任务需要非空调用 ID 和准备函数。")
        if not isinstance(self.capability, ToolCapability) or type(self.parallel_safe) is not bool:
            raise ValueError("工具调度声明必须来自已校验的注册代码。")
        if self.parallel_safe and self.capability is not ToolCapability.READ_ONLY:
            raise ValueError("只有明确声明安全的只读工具可以并行。")


@dataclass(frozen=True)
class ToolResult(Generic[T]):
    """保留原调用 ID 的动作结果；业务失败也可以作为普通返回值交付。"""

    call_id: str
    value: T


@dataclass
class _Worker(Generic[T]):
    """线程与完成值一起归调度器管理，Future 不承担线程回收。"""

    task: ToolTask[T]
    thread: Thread
    future: Future[T]
    finished: Event


class ToolScheduler:
    """有序预检、最多两个并发读取、按原序交付；串行动作构成前后屏障。"""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        cleanup_timeout_seconds: float = 2.0,
        cancel: CancellationToken | None = None,
    ) -> None:
        """取消等待上限按整组计算；超时报告残留线程，不能强行终止 Python 线程。"""
        if type(max_concurrent) is not int or max_concurrent not in {1, 2}:
            raise ValueError("只读工具并发上限只能为 1 或 2。")
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not math.isfinite(cleanup_timeout_seconds)
            or not 0 < cleanup_timeout_seconds <= 60
        ):
            raise ValueError("工具清理等待须大于 0 且不超过 60 秒。")
        self._max_concurrent = max_concurrent
        self._cleanup_timeout = cleanup_timeout_seconds
        self._cancel = cancel
        self._running = Lock()
        self._unfinished: tuple[str, ...] = ()

    def run(
        self,
        tasks: Sequence[ToolTask[T]],
        *,
        on_result: Callable[[ToolResult[T]], None] | None = None,
    ) -> list[ToolResult[T]]:
        """执行有限批次，预检与结果回调始终在调用线程运行。

        Args:
            tasks: 模型原顺序的任务；ID 在本批内唯一，非并行任务不能被跨越。
            on_result: 按输入顺序保存结果的回调；异常停止本批并清理正在运行的读取。

        Returns:
            与输入一一对应的结果。工具业务错误应由动作转换为普通返回值。

        Raises:
            BaseException: 准备、动作、回调或取消失败，清理后保留原异常。
            ToolCleanupError: 工作线程违反取消契约；原故障保留在异常链中。
        """
        if not self._running.acquire(blocking=False):
            raise RuntimeError("同一个工具调度器不能重入或同时处理两批任务。")
        try:
            if self._unfinished:
                raise ToolCleanupError(self._unfinished)
            batch = tuple(tasks)
            if len({task.call_id for task in batch}) != len(batch):
                raise ValueError("同批工具调用 ID 重复。")
            results: list[ToolResult[T]] = []

            def deliver(result: ToolResult[T]) -> None:
                if on_result:
                    on_result(result)
                results.append(result)

            token = CancellationToken()
            self._check_cancel(token)
            # 串行动作前后都不能留有读取线程，避免读取与修改同一文件互相穿插。
            for parallel, group in groupby(batch, key=lambda task: task.parallel_safe):
                if parallel:
                    self._parallel(tuple(group), token, deliver)
                else:
                    for task in group:
                        self._check_cancel(token)
                        action = task.prepare()
                        self._check_cancel(token)
                        value = action(self._cancel or token)
                        deliver(ToolResult(task.call_id, value))
            return results
        finally:
            self._running.release()

    def _parallel(
        self,
        tasks: tuple[ToolTask[T], ...],
        token: CancellationToken,
        deliver: Callable[[ToolResult[T]], None],
    ) -> None:
        """空闲槽及时派发下一项；完成值暂存，回调与返回仍按输入顺序。"""
        workers: dict[int, _Worker[T]] = {}
        completed: dict[int, T] = {}
        started = delivered = 0
        try:
            while delivered < len(tasks):
                self._check_cancel(token)
                for index, worker in tuple(workers.items()):
                    if worker.thread.is_alive():
                        continue
                    worker.thread.join()
                    # 预期业务失败由工具编码；程序异常则立即停止其他读取。
                    completed[index] = worker.future.result()
                    del workers[index]
                while delivered in completed:
                    self._check_cancel(token)
                    deliver(ToolResult(tasks[delivered].call_id, completed.pop(delivered)))
                    delivered += 1
                while started < len(tasks) and len(workers) < self._max_concurrent:
                    self._check_cancel(token)
                    task = tasks[started]
                    action = task.prepare()
                    self._check_cancel(token)
                    worker = self._worker(task, action, token)
                    workers[started] = worker
                    worker.thread.start()
                    started += 1
                if workers:
                    time.sleep(0.01)
        except BaseException:
            token.cancel()
            self._cleanup(workers.values())
            raise

    def _worker(
        self, task: ToolTask[T], action: ToolAction[T], token: CancellationToken
    ) -> _Worker[T]:
        """工作线程只产生值或异常；不运行准备函数、结果回调或日志写入。"""
        future: Future[T] = Future()
        finished = Event()

        def execute() -> None:
            try:
                token.check()
                future.set_result(action(token))
            except BaseException as error:
                future.set_exception(error)
            finally:
                finished.set()

        # 不合作的读取会被报告并使调度器失效；不能让残留线程阻止解释器退出。
        thread = Thread(target=execute, name=f"adam-read-{task.call_id}", daemon=True)
        return _Worker(task, thread, future, finished)

    def _cleanup(self, workers: Iterable[_Worker[T]]) -> None:
        """只等待一个总期限；超时不声称已回收，永久禁止此调度器继续派发。"""
        deadline = time.monotonic() + self._cleanup_timeout
        pending = tuple(workers)
        for worker in pending:
            if worker.thread.ident is None:
                continue
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    # 等动作的 finally 完成后再 join；再次 Ctrl+C 也不能跳过资源清理。
                    if worker.finished.wait(remaining):
                        worker.thread.join(max(0.0, deadline - time.monotonic()))
                    break
                except KeyboardInterrupt:
                    continue
        self._unfinished = tuple(
            worker.task.call_id
            for worker in pending
            if worker.thread.ident is not None
            and (not worker.finished.is_set() or worker.thread.is_alive())
        )
        if self._unfinished:
            raise ToolCleanupError(self._unfinished)

    def _check_cancel(self, token: CancellationToken) -> None:
        """父令牌仅接收取消；组内失败只停止本组，不反向取消其他任务。"""
        if self._cancel:
            self._cancel.check()
        token.check()

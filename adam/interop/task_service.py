"""本机实验的有界异步 Task 服务，取消依赖宿主回调协作。"""

from collections.abc import Callable
from copy import deepcopy
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted


class TaskService:
    """进程内任务生命周期；不共享 SessionStore，不支持重启恢复或流式。"""

    def __init__(
        self, handler: Callable[[str, CancellationToken], str], *, limit: int = 64
    ) -> None:
        self.handler = handler
        self.limit = limit
        self._lock = Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._threads: list[Thread] = []

    def submit(self, text: str) -> dict[str, Any]:
        """先创建服务端标识再运行；达到保留上限后拒绝新任务。"""
        with self._lock:
            if len(self._tasks) >= self.limit:
                raise ValueError("task capacity")
            task_id = uuid4().hex
            task = {
                "kind": "task",
                "id": task_id,
                "contextId": uuid4().hex,
                "status": {"state": "working"},
            }
            self._tasks[task_id] = task
            cancel = CancellationToken()
            self._tokens[task_id] = cancel
            thread = Thread(target=self._run, args=(task_id, text, cancel), daemon=True)
            self._threads.append(thread)
            result = deepcopy(task)
            thread.start()
            return result

    def get(self, task_id: str) -> dict[str, Any]:
        """返回固定任务快照；不存在的 ID 不创建替代任务。"""
        with self._lock:
            return deepcopy(self._tasks[task_id])

    def cancel(self, task_id: str) -> dict[str, Any]:
        """仅发送取消信号；回调尚未退出时继续返回 working。"""
        with self._lock:
            task = self._tasks[task_id]
            if task["status"]["state"] == "working":
                self._tokens[task_id].cancel()
            return deepcopy(task)

    def close(self) -> None:
        """请求所有任务停止并有限等待；未退出时明确报错，不宣称已取消。"""
        for token in self._tokens.values():
            token.cancel()
        for thread in self._threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("task callbacks did not stop")

    def _run(self, task_id: str, text: str, cancel: CancellationToken) -> None:
        """保存真实完成/失败/取消；取消状态仅在回调退出后发布。"""
        state, answer = "completed", None
        try:
            cancel.check()
            answer = self.handler(text, cancel)
            if not isinstance(answer, str) or len(answer.encode("utf-8")) > 65536:
                raise ValueError("invalid task output")
            cancel.check()
        except RequestInterrupted:
            state = "canceled"
        except Exception:
            state = "failed"
        with self._lock:
            task = self._tasks[task_id]
            task["status"] = {"state": state}
            if state == "completed":
                task["artifacts"] = [
                    {"artifactId": uuid4().hex, "parts": [{"kind": "text", "text": answer}]}
                ]

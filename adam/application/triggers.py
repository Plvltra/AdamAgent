"""宿主协调线程把一次性本地条件转成运行邮箱事件，不执行模型或命令。"""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.application.event_queue import EventQueue
from adam.application.timers import LocalTimers, Timer
from adam.contracts.events import EventKind
from adam.contracts.models import valid_session_id


class LocalTriggers:
    """每个会话独立持久记录；只向原运行投递，恢复后不会重绑旧提醒。"""

    def __init__(self, root: Path, query_background: Callable[..., dict[str, Any]]) -> None:
        self.root, self.query_background = root, query_background
        self._stores: dict[str, LocalTimers] = {}

    def schedule(self, owner: str, task_id: str, due_at: float, text: str) -> dict[str, Any]:
        """登记定时消息；宿主后续poll才观察到期，不承诺毫秒级提醒。"""
        return asdict(self._store(owner).schedule(task_id, due_at, text))

    def watch(self, owner: str, task_id: str, job_id: str, condition: str) -> dict[str, Any]:
        """监听有输出或进入终态；同运行/任务/条件只登记一次，合并高频输出。

        output包含登记前已经产生的输出；仅报告有输出，不把命令内容当授权。
        """
        if condition not in {"output", "completed"}:
            raise ValueError("condition must be output or completed")
        store = self._store(owner)
        for timer in store.list():
            if (timer.task_id, timer.job_id, timer.condition) == (task_id, job_id, condition):
                return asdict(timer)
        result = self.query_background(owner, job_id)
        if result.get("restored"):
            raise ValueError("cannot watch a restored background task")
        timer = store.schedule(
            task_id,
            0,
            f"后台任务{job_id}的{condition}条件已满足；请查询实际输出与退出码。",
            condition=condition,
            job_id=job_id,
        )
        return asdict(timer)

    def cancel(self, owner: str, timer_id: str) -> dict[str, Any]:
        """只取消当前会话尚未投递的条件，不取消后台进程。"""
        return asdict(self._store(owner).cancel(timer_id))

    def poll(self, owner: str, queue: EventQueue, now: float) -> int:
        """在安全点查询并持久接受事件；满队列保留pending，下次同ID去重。

        接受成功后再写delivered；保存失败向上抛出，不能继续模型请求。跨轮次条件过期。
        """
        store = self._store(owner)
        for timer in store.list():
            if timer.status == "pending" and timer.task_id != queue.task_id:
                store.finish(timer.timer_id, "expired")
        delivered = 0
        for timer in store.due(now):
            if timer.condition != "timer":
                result = self.query_background(owner, timer.job_id)
                if result.get("restored"):
                    store.finish(timer.timer_id, "expired")
                    continue
                terminal = result["status"] != "running"
                output = any(result.get(stream, {}).get("text") for stream in ("stdout", "stderr"))
                if not (terminal if timer.condition == "completed" else output):
                    if terminal:
                        store.finish(timer.timer_id, "expired")
                    continue
            try:
                queue.put(
                    EventKind.TIMER if timer.condition == "timer" else EventKind.TASK_UPDATE,
                    "local_trigger",
                    timer.timer_id,
                    timer.text,
                )
            except ValueError:
                # 队列已封口或容量耗尽时不伪造已投递；持久失败不在此吞掉。
                break
            store.finish(timer.timer_id, "delivered")
            delivered += 1
        return delivered

    def records(self, owner: str) -> tuple[Timer, ...]:
        """只读已登记状态，不触发条件或新建运行。"""
        return self._store(owner).list()

    def close(self) -> None:
        """释放所有会话定时锁，不执行遗留条件。"""
        for store in self._stores.values():
            store.close()
        self._stores.clear()

    def _store(self, owner: str) -> LocalTimers:
        if not valid_session_id(owner):
            raise ValueError("invalid trigger session")
        if owner not in self._stores:
            self._stores[owner] = LocalTimers(self.root / owner)
        return self._stores[owner]

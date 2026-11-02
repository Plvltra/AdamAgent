"""一次性本地定时记录；宿主显式轮询，没有后台线程或自动执行权限。"""

import json
import math
import os
import secrets
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from filelock import FileLock, Timeout

from adam.contracts.errors import StorageError


@dataclass(frozen=True)
class Timer:
    """due_at为UTC Unix秒；task_id固定目标运行，不允许投递给下一轮。"""

    timer_id: str
    task_id: str
    due_at: float
    text: str
    status: str = "pending"


class LocalTimers:
    """仅宿主协调线程使用，独占定时目录；打开后必须close，恢复不触发回调。"""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._closed = False
        self._items: dict[str, Timer] = {}
        self._lock = FileLock(self.root / "timers.lock", timeout=0)
        try:
            self._path("timers.lock")
            self.root.mkdir(parents=True, exist_ok=True)
            self._lock.acquire()
            path = self._path("timers.json")
            if path.exists():
                if path.stat().st_size > 2_000_000:
                    raise ValueError("timer file too large")
                rows = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(rows, list) or len(rows) > 128:
                    raise ValueError("invalid timer records")
                for row in rows:
                    timer = Timer(**row)
                    self._validate(timer)
                    if timer.timer_id in self._items:
                        raise ValueError("duplicate timer ID")
                    self._items[timer.timer_id] = timer
        except (OSError, ValueError, TypeError, Timeout) as error:
            self._lock.release()
            raise StorageError("定时记录无法打开或已由另一宿主管理。") from error
        except BaseException:
            self._lock.release()
            raise

    def schedule(self, task_id: str, due_at: float, text: str) -> Timer:
        """保存一次性提醒后返回；到期时间允许在过去，下一次due即可查询。"""
        self._check_open()
        if len(self._items) >= 128:
            raise ValueError("timer capacity exhausted")
        timer = Timer(secrets.token_hex(8), task_id, due_at, text)
        self._validate(timer)
        self._store({**self._items, timer.timer_id: timer})
        return timer

    def due(self, now: float) -> tuple[Timer, ...]:
        """只读取得到期pending记录；查询不代表已投递或已执行。"""
        self._check_open()
        self._time(now)
        return tuple(
            sorted(
                (t for t in self._items.values() if t.status == "pending" and t.due_at <= now),
                key=lambda t: (t.due_at, t.timer_id),
            )
        )

    def finish(self, timer_id: str, status: str) -> Timer:
        """结算为delivered/cancelled/expired；同终态幂等，禁止改写已有终态。

        delivered只表示宿主已接受事件，不表示模型执行成功；调用者先取得投递回执。
        """
        self._check_open()
        if status not in {"delivered", "cancelled", "expired"}:
            raise ValueError("invalid timer outcome")
        timer = self._items[timer_id]
        if timer.status == status:
            return timer
        if timer.status != "pending":
            raise ValueError("timer already finished")
        updated = replace(timer, status=status)
        self._store({**self._items, timer_id: updated})
        return updated

    def cancel(self, timer_id: str) -> Timer:
        """取消尚未投递的提醒；已投递的事件不能通过此接口撤回。"""
        return self.finish(timer_id, "cancelled")

    def list(self) -> tuple[Timer, ...]:
        """读取包括终态在内的不可变快照。"""
        self._check_open()
        return tuple(self._items.values())

    def close(self) -> None:
        """释放独占锁；不触发到期任务，可重复关闭。"""
        self._closed = True
        self._lock.release()

    def _store(self, items: dict[str, Timer]) -> None:
        temporary = self._path("timers.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump([asdict(item) for item in items.values()], stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path("timers.json"))
        except OSError as error:
            raise StorageError("定时记录保存失败，未更新内存状态。") from error
        self._items = items

    def _path(self, name: str) -> Path:
        path = self.root / name
        if path.resolve() != path:
            raise StorageError("定时记录目录不能通过链接重定向。")
        return path

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("timer store closed")

    @staticmethod
    def _time(value: float) -> None:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("time must be finite Unix seconds")

    @classmethod
    def _validate(cls, timer: Timer) -> None:
        for value in (timer.timer_id, timer.task_id):
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or not value.isascii()
                or any(not (c.isalnum() or c in "_.:-") for c in value)
            ):
                raise ValueError("invalid timer identity")
        cls._time(timer.due_at)
        if not isinstance(timer.text, str) or not 1 <= len(timer.text) <= 8192:
            raise ValueError("invalid timer message")
        if timer.status not in {"pending", "delivered", "cancelled", "expired"}:
            raise ValueError("invalid timer status")

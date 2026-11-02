"""同一用户临时目录的桌面输入租约；跨线程/进程互斥，不等待并抢占已有调用。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir

from filelock import FileLock, Timeout

from adam.contracts.errors import ToolError


class DesktopLease:
    """一次调用持有到批准、派发和观察收尾；崩溃由操作系统释放文件锁。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(gettempdir()) / "adam-desktop-input.lock"

    @contextmanager
    def hold(self) -> Iterator[None]:
        """竞争立即拒绝，不排队执行过时动作；异常也释放，锁文件可保留。"""
        lock = FileLock(self.path)
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise ToolError(
                "Desktop input is leased by another operation; observe again later"
            ) from error
        try:
            yield
        finally:
            lock.release()

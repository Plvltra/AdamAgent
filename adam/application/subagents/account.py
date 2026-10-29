"""把当前线程拥有的会话日志接到整组 token 额度。"""

from collections.abc import Iterator
from contextlib import contextmanager

from adam.application.subagents.budget import SharedTokenBudget
from adam.session.storage import SessionStore


class SessionAccount:
    """只刷新自己的日志，不读取其他正在写入的子会话。"""

    def __init__(self, store: SessionStore, session_id: str, budget: SharedTokenBudget) -> None:
        self.store, self.session_id, self.budget = store, session_id, budget
        self.owner = str(store.root / session_id)

    def check(self) -> None:
        """先刷新本会话快照，其他在途请求仍只占预留，不被误判成消费未知。"""
        self._refresh()
        self.budget.check()

    def usage(self) -> tuple[int, bool]:
        """返回主会话及全部子会话的已知累计消费。"""
        self._refresh()
        return self.budget.usage()

    @contextmanager
    def request(self, estimate: int) -> Iterator[None]:
        """请求开始前预留，日志收尾后结算；日志读取失败时保守保留未知消费。"""
        self._refresh()
        with self.budget.reserve(estimate):
            try:
                yield
            except BaseException as error:
                try:
                    self._refresh()
                except Exception:
                    error.add_note("共享额度结算读取失败；停止新增请求，不清零消费。")
                raise
            else:
                self._refresh()

    def _refresh(self) -> None:
        try:
            tokens, unknown = self.store.usage(self.session_id)
        except Exception:
            # 无法确认该成员的新快照时，另立未知标记，不用零覆盖既有已知消费。
            self.budget.update(self.owner + ":unreadable", 0, True)
            raise
        self.budget.update(self.owner, tokens, unknown)

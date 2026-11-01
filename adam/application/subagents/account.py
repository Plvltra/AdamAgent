"""把当前线程拥有的会话日志接到整组 token 额度。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from adam.application.subagents.budget import SharedTokenBudget
from adam.contracts.runtime import TokenState
from adam.session.storage import SessionStore


class SessionAccount:
    """只刷新自己的日志，不读取其他正在写入的子会话。"""

    def __init__(self, store: SessionStore, session_id: str, budget: SharedTokenBudget) -> None:
        self.store, self.session_id, self.budget = store, session_id, budget
        self.owner = str(store.root / session_id)
        self.on_snapshot: Callable[[str, dict[str, Any]], None] | None = None

    def check(self) -> None:
        """先刷新本会话快照，其他在途请求仍只占预留，不被误判成消费未知。"""
        self._refresh()
        self.budget.check(observe=self._observer("check"))

    def usage(self) -> tuple[int, bool]:
        """返回主会话及全部子会话的已知累计消费。"""
        self._refresh()
        return self.budget.usage(observe=self._observer("usage"))

    def snapshot(self) -> TokenState:
        """刷新本会话后复制整组消费和预留；输入观察者另行保存，不触发执行决策。"""
        self._refresh()
        return self.budget.snapshot()

    @contextmanager
    def request(self, estimate: int) -> Iterator[None]:
        """请求开始前预留，日志收尾后结算；日志读取失败时保守保留未知消费。"""
        self._refresh()
        with self.budget.reserve(estimate, observe=self._observer("reserve", estimate)):
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

    def _observer(
        self, operation: str, estimate: int | None = None
    ) -> Callable[[dict[str, Any]], None] | None:
        """宿主可观察当前线程真实使用的共享账本；不从其他线程读取会话文件。"""
        callback = self.on_snapshot
        if callback is None:
            return None
        return lambda state: callback(
            operation, {"owner": self.owner, "estimate": estimate, **state}
        )

    def _refresh(self) -> None:
        try:
            tokens, unknown = self.store.usage(self.session_id)
        except Exception:
            # 无法确认该成员的新快照时，另立未知标记，不用零覆盖既有已知消费。
            self.budget.update(self.owner + ":unreadable", 0, True)
            raise
        self.budget.update(self.owner, tokens, unknown)


def restore_account(store: SessionStore, session_id: str, maximum: int) -> SessionAccount:
    """在主会话持锁且不派生新子任务的入口恢复整组额度，不漏掉旧子会话消费。

    子日志逐个取得原写锁并按 SessionStore 规则恢复；缺失、占用或损坏时停止，
    不用零替代。这里只结算日志快照，不改委派索引、不重发任务。
    返回账本仅供当前入口使用；调用期间不能另启共享同一主会话的子任务。
    """
    from adam.contracts.errors import StorageError
    from adam.session.delegation import DelegationStore

    if store.active_session_id != session_id:
        raise StorageError("恢复共享额度必须持有当前主会话写锁。")
    budget = SharedTokenBudget(maximum)
    root = store.root / "_subagents" / session_id
    if root.exists():
        index = DelegationStore(root)
        with SessionStore(root / "sessions") as children:
            for agent in index.agents():
                children.select_session(agent.agent_id)
                budget.update(str(children.root / agent.agent_id), *children.usage(agent.agent_id))
    account = SessionAccount(store, session_id, budget)
    account.usage()
    return account

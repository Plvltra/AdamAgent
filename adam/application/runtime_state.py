"""把宿主的窄查询接口汇总为输入状态；不读取其他子线程日志或操作执行器内部字段。"""

from collections.abc import Callable
from pathlib import Path

from adam.contracts.execution import ExecutionMode
from adam.contracts.runtime import CountState, FailureState, RuntimeState, TokenState
from adam.session.storage import SessionStore


class RuntimeStateReader:
    """在拥有当前会话的线程读取；动态来源由组装入口注入，不持有全局配置。"""

    def __init__(
        self,
        store: SessionStore,
        workspace: Path,
        maximum_tokens: int,
        mode: Callable[[], ExecutionMode],
        failures: Callable[[str, str], FailureState],
        *,
        tokens: Callable[[str], TokenState] | None = None,
        pending: Callable[[str], tuple[str, ...] | None] | None = None,
    ) -> None:
        """绑定可信来源；tokens 缺省只用于无共享账本，pending 缺省表示没有子任务能力。

        查询异常由调用方收尾，不将读取失败当作零消费或无待收集任务。
        在组装时解析相对工作目录，后续状态不会随进程 cwd 的变化漂移。
        """
        self.store = store
        self.workspace = str(workspace.resolve())
        self.maximum_tokens = maximum_tokens
        self.mode, self.failures, self.tokens, self.pending = mode, failures, tokens, pending

    def __call__(
        self, session_id: str, turn_id: str, requests: CountState, tools: CountState
    ) -> RuntimeState:
        """取得本次输入的快照；仅单会话模式可把不存在的并发预留确认为零。"""
        if self.tokens is not None:
            tokens = self.tokens(session_id)
        else:
            used, unknown = self.store.usage(session_id)
            tokens = TokenState(self.maximum_tokens, used, unknown, 0)
        return RuntimeState(
            requests,
            tools,
            tokens,
            self.failures(session_id, turn_id),
            self.workspace,
            self.mode(),
            self.pending(session_id) if self.pending else (),
        )

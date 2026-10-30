"""用户撤销的应用流程；复用编辑权限与原子写入，不调用模型或修改聊天历史。"""

import secrets
from collections.abc import Callable
from time import monotonic_ns
from typing import Any

from adam.contracts.checkpoints import UndoResult
from adam.contracts.errors import AgentError, EditLogError, StorageError, ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode
from adam.session.storage import SessionStore
from adam.tool_calls.edit_plan import EditConflictError
from adam.tool_calls.edit_tools import EditTools


class UndoService:
    """协调最近一次成功编辑的逆向批准与留痕；快照本身不授予当前写入权限。"""

    def __init__(
        self,
        store: SessionStore,
        editor: EditTools,
        mode: Callable[[], ExecutionMode],
    ) -> None:
        self._store, self._editor, self._mode = store, editor, mode

    def undo(self, session_id: str) -> UndoResult:
        """撤销最近尚未撤销的成功编辑；每次均重新批准，不自动回退到更早的损坏候选。

        调用方须保证本终端没有运行中的任务。保存失败立即停止；文件已开始写入而缺少
        结束记录时保留 unknown。Ctrl+C 在开始前记中断，开始后不猜结果或再次写入。
        """
        check_tool_mode(self._mode(), ToolCapability.EDIT_FILE)
        checkpoint = self._store.undo_checkpoint(
            session_id, max_bytes=self._editor.limits.max_text_file_bytes
        )
        if checkpoint is None:
            raise AgentError("当前会话没有尚未撤销的成功文件编辑。")
        undo_id = secrets.token_hex(16)
        link = {"undo_id": undo_id, "edit_id": checkpoint.edit_id}
        started_ns = monotonic_ns()
        self._record(
            session_id,
            "undo_requested",
            {
                **link,
                "path": str(checkpoint.path),
                "before_version": checkpoint.target_version,
                "target_version": checkpoint.before_version,
                "source_turn_id": checkpoint.turn_id,
                "source_request_id": checkpoint.request_id,
                "source_tool_call_id": checkpoint.tool_call_id,
            },
        )
        started = False
        try:
            plan = self._editor.prepare_restore(checkpoint)
            self._record(session_id, "undo_prepared", {**link, "diff": plan.diff})
            if self._editor.confirm is None or not self._editor.confirm(plan):
                self._finish(session_id, link, "denied", started_ns)
                return UndoResult(undo_id, checkpoint.edit_id, checkpoint.path, "denied")
            self._record(session_id, "undo_started", link)
            started = True
            self._editor.apply(plan)
        except (KeyboardInterrupt, EOFError) as error:
            if not started:
                try:
                    self._finish(
                        session_id, link, "interrupted", started_ns, error="用户在写入前取消撤销。"
                    )
                except EditLogError:
                    # 保留原取消；若开始记录已经落盘，非法中断转换也不能把 unknown 改掉。
                    error.add_note("撤销中断记录未确认保存，请检查日志；未自动重做。")
            raise
        except ToolError as error:
            self._finish(
                session_id,
                link,
                "conflict" if isinstance(error, EditConflictError) else "failed",
                started_ns,
                error=str(error),
            )
            raise
        self._finish(
            session_id, link, "applied", started_ns, after_version=checkpoint.before_version
        )
        return UndoResult(undo_id, checkpoint.edit_id, checkpoint.path, "applied")

    def _finish(
        self,
        session_id: str,
        link: dict[str, str],
        status: str,
        started_ns: int,
        *,
        after_version: str | None = None,
        error: str | None = None,
    ) -> None:
        """先保存实际结果再向调用者返回；日志故障不能把已写入文件说成未执行。"""
        self._record(
            session_id,
            "undo_finished",
            {
                **link,
                "status": status,
                "after_version": after_version,
                "duration_ms": max(0, (monotonic_ns() - started_ns) // 1_000_000),
                "error": error,
            },
        )

    def _record(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """存储和关联故障统一停止撤销；不在此重试或代替用户执行补偿。"""
        try:
            self._store.append_undo_event(session_id, kind, payload)
        except (StorageError, ValueError) as error:
            raise EditLogError(
                "撤销记录保存失败；已停止，请核对文件与日志，未自动重做。"
            ) from error

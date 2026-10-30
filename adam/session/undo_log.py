"""用户撤销的纯日志投影；独立于模型轮次，始终关联原成功编辑。"""

import re
from typing import Any


class UndoLog:
    """requested/prepared 尚未写文件；started 后缺少结果保持 unknown，重启不推断。"""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}

    def latest_edit(
        self, edits: dict[str, dict[str, Any]], executions: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        """选择最近尚未撤销的成功编辑；有未确认副作用时禁止跨过它继续修改。"""
        if self.unfinished() or any(
            item["status"] == "unknown" for item in (*edits.values(), *executions.values())
        ):
            raise ValueError("unconfirmed operation blocks undo")
        undone = {
            item["edit_id"] for item in self.operations.values() if item["status"] == "applied"
        }
        return next(
            (
                item
                for item in reversed(edits.values())
                if item["status"] == "applied" and item["edit_id"] not in undone
            ),
            None,
        )

    def apply(
        self,
        kind: str,
        payload: dict[str, Any],
        edits: dict[str, dict[str, Any]],
        executions: dict[str, dict[str, Any]],
    ) -> None:
        """校验来源、逆向版本和转换；不能重复撤销或把未知结果记成未执行。"""
        undo_id = payload.get("undo_id")
        if not isinstance(undo_id, str) or re.fullmatch(r"[0-9a-f]{32}", undo_id) is None:
            raise ValueError("invalid undo id")
        if kind == "undo_requested":
            edit = self.latest_edit(edits, executions)
            if undo_id in self.operations or edit is None:
                raise ValueError("invalid undo source")
            expected = {
                "edit_id": edit["edit_id"],
                "path": edit["path"],
                "before_version": edit["after_version"],
                "target_version": edit["before_version"],
                **{
                    f"source_{field}": edit[field]
                    for field in ("turn_id", "request_id", "tool_call_id")
                },
            }
            if payload != {"undo_id": undo_id, **expected}:
                raise ValueError("undo source or version mismatch")
            self.operations[undo_id] = {
                **payload,
                "status": "requested",
                "diff": None,
                "after_version": None,
                "duration_ms": None,
                "error": None,
            }
            return
        previous = self.operations.get(undo_id)
        if previous is None or payload.get("edit_id") != previous["edit_id"]:
            raise ValueError("unknown or mismatched undo")
        link = {"undo_id", "edit_id"}
        if kind == "undo_prepared":
            if (
                payload.keys() != link | {"diff"}
                or previous["status"] != "requested"
                or not isinstance(payload["diff"], str)
                or not payload["diff"]
            ):
                raise ValueError("invalid undo diff")
            previous.update(diff=payload["diff"], status="prepared")
        elif kind == "undo_started":
            if payload.keys() != link or previous["status"] != "prepared":
                raise ValueError("undo has no approved plan")
            previous.update(status="unknown", approved=True)
        elif kind == "undo_finished":
            self._finish(previous, payload)
        else:
            raise ValueError("unknown undo event")

    def unfinished(self) -> bool:
        """包含尚未开始和结果未知的撤销；这两种情况都不能并发发起另一次操作。"""
        return any(
            item["status"] in {"requested", "prepared", "unknown"}
            for item in self.operations.values()
        )

    def pending_ids(self) -> list[str]:
        """只列出确定尚未进入写入阶段的操作，供持锁恢复标记中断。"""
        return [
            key
            for key, item in self.operations.items()
            if item["status"] in {"requested", "prepared"}
        ]

    def recover(self) -> None:
        """未开始的撤销记为中断；unknown 保持原状，不读取目标文件猜测结果。"""
        for undo_id in self.pending_ids():
            self.operations[undo_id].update(
                status="interrupted",
                duration_ms=None,
                error="上次撤销未进入写入阶段；未自动重做。",
            )

    @staticmethod
    def _finish(previous: dict[str, Any], payload: dict[str, Any]) -> None:
        """只有已开始的操作可确认成功；开始后不能用拒绝/中断掩盖未知副作用。"""
        status = payload.get("status")
        allowed = {
            "requested": {"conflict", "failed", "interrupted"},
            "prepared": {"denied", "conflict", "failed", "interrupted"},
            "unknown": {"applied", "conflict", "failed"},
        }
        if not isinstance(status, str) or status not in allowed.get(previous["status"], set()):
            raise ValueError("invalid undo transition")
        if payload.keys() != {
            "undo_id",
            "edit_id",
            "status",
            "after_version",
            "duration_ms",
            "error",
        }:
            raise ValueError("invalid undo result fields")
        if type(payload["duration_ms"]) is not int or payload["duration_ms"] < 0:
            raise ValueError("invalid undo duration")
        if payload["error"] is not None and not isinstance(payload["error"], str):
            raise ValueError("invalid undo error")
        expected = previous["target_version"] if status == "applied" else None
        if payload["after_version"] != expected:
            raise ValueError("invalid restored version")
        previous.update(payload)

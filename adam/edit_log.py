"""文件编辑事件的纯状态投影；计划、开始写入和确认结果分别记录。"""

import re
from typing import Any


class EditLog:
    """已开始而未确认结果的修改保持 unknown；恢复不推断磁盘内容。"""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}

    def apply(self, kind: str, payload: dict[str, Any]) -> None:
        """校验同一编辑的关联和转换；父工具存在性由 SessionLog 校验。"""
        edit_id = payload.get("edit_id")
        if not isinstance(edit_id, str) or not edit_id:
            raise ValueError("invalid edit id")
        previous = self.operations.get(edit_id)
        if kind == "edit_planned":
            if (
                previous is not None
                or not isinstance(payload.get("path"), str)
                or not payload["path"]
            ):
                raise ValueError("invalid edit plan")
            for field in ("before_version", "target_version"):
                if (
                    not isinstance(payload.get(field), str)
                    or re.fullmatch("[0-9a-f]{64}", payload[field]) is None
                ):
                    raise ValueError("invalid file version")
            if not isinstance(payload.get("diff"), str) or not payload["diff"]:
                raise ValueError("missing edit diff")
            if payload["before_version"] == payload["target_version"]:
                raise ValueError("unchanged edit")
            self.operations[edit_id] = {**payload, "status": "planned", "after_version": None}
            return
        if previous is None or any(
            payload.get(field) != previous[field]
            for field in ("turn_id", "request_id", "tool_call_id")
        ):
            raise ValueError("unknown or mismatched edit")
        if kind == "edit_started":
            if previous["status"] != "planned":
                raise ValueError("edit already started")
            previous.update(status="unknown", approved=True)
            return
        if kind != "edit_finished":
            raise ValueError("unknown edit event")
        for field in ("path", "before_version", "target_version", "diff"):
            if field in payload and payload[field] != previous[field]:
                raise ValueError("edit plan changed")
        status = payload.get("status")
        expected = "planned" if status == "denied" else "unknown"
        if previous["status"] != expected or status not in {
            "denied",
            "applied",
            "conflict",
            "failed",
        }:
            raise ValueError("invalid edit transition")
        if type(payload.get("duration_ms")) is not int or payload["duration_ms"] < 0:
            raise ValueError("invalid edit duration")
        if payload.get("error") is not None and not isinstance(payload["error"], str):
            raise ValueError("invalid edit error")
        if payload.get("after_version") != (
            previous["target_version"] if status == "applied" else None
        ):
            raise ValueError("invalid applied version")
        previous.update(payload)

    def pending_ids(self) -> list[str]:
        return [key for key, item in self.operations.items() if item["status"] == "planned"]

    def recover(self) -> None:
        """无 started 的计划确定未写入；有 started 的未知结果保持未知。"""
        for edit_id in self.pending_ids():
            self.operations[edit_id]["status"] = "not_started"

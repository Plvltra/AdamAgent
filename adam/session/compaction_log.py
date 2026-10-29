"""摘要事件的纯状态投影；请求结算和摘要提交分别验证，不访问文件。"""

from typing import Any

from adam.contracts.history import Summary
from adam.session.outcome import outcome_details


class CompactionLog:
    """记录独立摘要操作；未结算请求保持未知，只有完成事件激活新摘要。"""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self.summary: Summary | None = None
        self.tokens = 0

    @property
    def unknown_usage(self) -> bool:
        return any(not op["settled"] or op.get("usage") is None for op in self.operations.values())

    def pending_ids(self) -> list[str]:
        """返回尚未结束的操作，供持锁恢复关闭。"""
        return [key for key, op in self.operations.items() if op["status"] == "in_progress"]

    def recover(self, details: dict[str, Any]) -> None:
        """关闭遗留操作；请求未结算仍未知，已提交摘要保持有效。"""
        for key in self.pending_ids():
            operation = self.operations[key]
            operation.update(details)
            if operation["request"]["status"] == "in_progress":
                operation["request"].update(details)

    def apply(self, kind: str, payload: dict[str, Any], history_ids: list[str]) -> None:
        """校验来源和状态后更新投影；非法关联抛出 ValueError，不部分启用摘要。"""
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("invalid compaction id")
        if kind == "compaction_started":
            self._start(operation_id, payload, history_ids)
            return
        if not {"status", "reason", "duration_ms", "error"}.issubset(payload):
            raise ValueError("missing compaction outcome")
        operation = self.operations.get(operation_id)
        if operation is None or operation["status"] != "in_progress":
            raise ValueError("unknown or completed compaction")
        if kind == "compaction_usage":
            self._settle(operation, payload)
        elif kind == "compaction_completed":
            text = payload.get("text")
            if (
                not operation["settled"]
                or operation["request"]["status"] != "succeeded"
                or operation["usage"] is None
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError("invalid summary commit")
            details = outcome_details(payload, "succeeded")
            if payload.get("status") != "succeeded":
                raise ValueError("invalid summary status")
            covered = self.summary.covered_turn_ids if self.summary else ()
            self.summary = Summary(
                operation_id, text, covered + tuple(operation["source_turn_ids"])
            )
            operation.update(details, text=text)
        elif kind == "compaction_finished":
            status = payload.get("status")
            if status not in {"failed", "interrupted"}:
                raise ValueError("invalid compaction failure")
            operation.update(outcome_details(payload, status))
        else:
            raise ValueError("unknown compaction event")

    def _start(self, key: str, payload: dict[str, Any], history_ids: list[str]) -> None:
        """新摘要只能接续最近已提交摘要，覆盖最早的连续未覆盖历史。"""
        if key in self.operations or self.pending_ids():
            raise ValueError("duplicate or pending compaction")
        previous = self.summary.summary_id if self.summary else None
        covered = len(self.summary.covered_turn_ids) if self.summary else 0
        sources = payload.get("source_turn_ids")
        if (
            payload.get("previous_summary_id") != previous
            or not isinstance(sources, list)
            or not sources
            or sources != history_ids[covered : covered + len(sources)]
        ):
            raise ValueError("invalid summary sources")
        if not isinstance(payload.get("model"), str):
            raise ValueError("invalid summary model")
        for field in ("max_output_tokens", "estimated_input_tokens"):
            if type(payload.get(field)) is not int or payload[field] <= 0:
                raise ValueError("invalid summary budget")
        self.operations[key] = {
            **payload,
            "status": "in_progress",
            "settled": False,
            "request": {"status": "in_progress"},
            "usage": None,
        }

    def _settle(self, operation: dict[str, Any], payload: dict[str, Any]) -> None:
        """保存一次请求的结果和消费；失败或用量未知不激活摘要。"""
        if operation["settled"]:
            raise ValueError("duplicate compaction usage")
        inputs, outputs = payload.get("input_tokens"), payload.get("output_tokens")
        if "input_tokens" not in payload or "output_tokens" not in payload:
            raise ValueError("missing compaction usage")
        if inputs is None and outputs is None:
            usage = None
        elif type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
            usage = {"input_tokens": inputs, "output_tokens": outputs}
            self.tokens += inputs + outputs
        else:
            raise ValueError("invalid compaction usage")
        status = payload.get("status")
        if status not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("invalid compaction request status")
        if payload.get("reason") == "api_not_sent" and (
            status != "failed" or usage != {"input_tokens": 0, "output_tokens": 0}
        ):
            raise ValueError("unsent summary must have zero usage")
        operation.update(settled=True, usage=usage, request=outcome_details(payload, status))

"""审查请求及结果的纯投影；重启与轮次结束只关闭未完成状态，不重跑审查。"""

from typing import Any

from adam.contracts.review import validate_request, validate_result


class ReviewLog:
    """子任务结束与审查结论分开记录，保留各次审查和文件版本。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def request(self, payload: dict[str, Any]) -> None:
        """同轮串行登记，限制在首次请求固定；重复 ID 或并行审查拒绝。"""
        validate_request(payload)
        previous = [r for r in self.records.values() if r["turn_id"] == payload["turn_id"]]
        if payload["review_id"] in self.records or any(r["status"] == "pending" for r in previous):
            raise ValueError("duplicate or parallel review")
        if payload["round"] != len(previous) + 1 or (
            previous and payload["max_rounds"] != previous[0]["max_rounds"]
        ):
            raise ValueError("invalid review rounds")
        self.records[payload["review_id"]] = {**payload, "status": "pending", "result": None}

    def finish(self, payload: dict[str, Any]) -> None:
        """保存程序取得的子证据快照；模型工具只提供 review_id，不能传入此结果。"""
        if set(payload) != {
            "review_id",
            "turn_id",
            "request_id",
            "tool_call_id",
            "status",
            "reason",
            "child_turn_id",
            "reads",
            "result",
        }:
            raise ValueError("invalid review finish fields")
        current = self.records.get(payload["review_id"])
        if (
            current is None
            or current["status"] != "pending"
            or current["turn_id"] != payload["turn_id"]
        ):
            raise ValueError("review is not pending")
        state = payload["status"]
        if state not in {"completed", "invalid", "failed", "interrupted", "stale"}:
            raise ValueError("invalid review state")
        if not isinstance(payload["reason"], str) or not 1 <= len(payload["reason"]) <= 500:
            raise ValueError("invalid review reason")
        reads = payload["reads"]
        if not isinstance(reads, list) or len(reads) > 64:
            raise ValueError("invalid reviewer reads")
        for read in reads:
            if not isinstance(read, dict) or set(read) != {
                "tool_call_id",
                "path",
                "version",
                "lines",
                "complete",
            }:
                raise ValueError("invalid reviewer read")
            if not all(
                isinstance(read[k], str) and read[k] for k in ("tool_call_id", "path", "version")
            ):
                raise ValueError("invalid reviewer identity")
            if type(read["complete"]) is not bool:
                raise ValueError("invalid read completeness")
            if not isinstance(read["lines"], list) or any(
                type(line) is not int or line < 1 for line in read["lines"]
            ):
                raise ValueError("invalid reviewer lines")
        if state == "completed":
            if not isinstance(payload["child_turn_id"], str) or not payload["child_turn_id"]:
                raise ValueError("missing reviewer turn")
            validate_result(payload["result"], reads, current["targets"])
        elif payload["result"] is not None:
            raise ValueError("failed review cannot claim a result")
        current.update(
            {key: payload[key] for key in ("status", "reason", "child_turn_id", "reads", "result")}
        )

    def interrupt(self, turn_id: str) -> None:
        """原始请求和子日志仍保留；父轮结束或恢复后不再显示正在审查。"""
        for item in self.records.values():
            if item["turn_id"] == turn_id and item["status"] == "pending":
                item.update(status="interrupted", reason="parent_turn_ended")

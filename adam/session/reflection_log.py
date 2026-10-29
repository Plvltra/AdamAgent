"""反思结果的纯投影；请求成功、建议可用和经验有效是不同事实。"""

from typing import Any

from adam.contracts.reflection import Feedback, decode_reflection


class ReflectionLog:
    """保存已完成、无效或跳过的反思；在途请求由原请求恢复规则处理。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}

    def apply(
        self,
        payload: dict[str, Any],
        feedback: dict[str, Feedback],
        requests: dict[str, dict[str, Any]],
    ) -> None:
        """校验反思与实际请求；格式降级必须有已结算的可靠用量。"""
        if set(payload) != {
            "reflection_id",
            "turn_id",
            "feedback_id",
            "request_id",
            "status",
            "reason",
            "result",
        }:
            raise ValueError("invalid reflection fields")
        identity = payload["reflection_id"]
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity) <= 128
            or identity in self.records
        ):
            raise ValueError("invalid reflection id")
        source = feedback.get(payload["feedback_id"])
        if source is None or source.turn_id != payload["turn_id"]:
            raise ValueError("invalid reflection feedback")
        state = payload["status"]
        if state == "skipped":
            if (
                payload["request_id"] is not None
                or payload["result"] is not None
                or payload["reason"] not in {"request_budget", "input_budget"}
            ):
                raise ValueError("invalid skipped reflection")
        else:
            request = requests.get(payload["request_id"], {})
            context = request.get("context", {})
            if (
                request.get("turn_id") != source.turn_id
                or context.get("strategy") != "reflection_v1"
                or context.get("feedback_id") != source.feedback_id
                or not request.get("settled")
                or request.get("usage") is None
                or None in request["usage"]
                or any(r["request_id"] == payload["request_id"] for r in self.records.values())
            ):
                raise ValueError("invalid reflection request")
            if state == "completed":
                if request["status"] != "succeeded" or payload["reason"] != "completed":
                    raise ValueError("invalid completed reflection")
                decode_reflection(payload["result"])
            elif state == "invalid":
                if (
                    payload["result"] is not None
                    or payload["reason"] != "invalid_response"
                    or (
                        request["status"] != "succeeded"
                        and request.get("reason") != "invalid_response"
                    )
                ):
                    raise ValueError("invalid reflection failure")
            else:
                raise ValueError("invalid reflection status")
        self.records[identity] = dict(payload)

    def decide(self, payload: dict[str, Any], turns: dict[str, dict[str, Any]]) -> None:
        """用户在轮次结束后接受或丢弃候选；状态只能决定一次，重复相同决策幂等。"""
        if set(payload) != {"reflection_id", "action", "memory_id"}:
            raise ValueError("invalid experience decision")
        identity = payload["reflection_id"]
        candidate = self.records.get(identity)
        if (
            candidate is None
            or candidate["status"] != "completed"
            or not candidate["result"]["lesson"].strip()
            or turns[candidate["turn_id"]]["status"] == "in_progress"
        ):
            raise ValueError("experience requires finished candidate")
        if payload["action"] not in {"accepted", "discarded"}:
            raise ValueError("invalid experience action")
        memory_id = payload["memory_id"]
        if payload["action"] == "accepted":
            if not isinstance(memory_id, str) or len(memory_id) != 8:
                raise ValueError("invalid experience memory")
        elif memory_id is not None:
            raise ValueError("discard cannot save memory")
        if identity in self.decisions and self.decisions[identity] != payload:
            raise ValueError("experience already decided")
        self.decisions[identity] = dict(payload)

    def permits_failed_request(self, request_id: str) -> bool:
        """仅已明确记录的反思格式失败允许继续提交聊天，其他请求失败仍阻止提交。"""
        return any(
            item["request_id"] == request_id and item["status"] == "invalid"
            for item in self.records.values()
        )

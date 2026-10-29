"""计划事件的纯回放；不保存独立文件，沿用会话日志的原子追加和恢复。"""

from typing import Any

from adam.contracts.planning import PlanReport, PlanSnapshot, decode_plan, is_replan


class PlanLog:
    """按轮次保留完整版本；轮次终态另从 SessionLog 取得，不自动恢复执行。"""

    def __init__(self) -> None:
        self.versions: dict[str, list[PlanSnapshot]] = {}

    def apply(self, payload: dict[str, Any]) -> None:
        """校验版本及重规划计数后更新；调用方先校验所属轮次和工具来源。"""
        current = decode_plan(payload)
        history = self.versions.get(current.turn_id, [])
        if any(plan.tool_call_id == current.tool_call_id for plan in history):
            raise ValueError("duplicate plan update source")
        previous = history[-1] if history else None
        if current.version != (previous.version + 1 if previous else 1):
            raise ValueError("invalid plan version")
        if previous:
            if (current.max_steps, current.max_replans) != (
                previous.max_steps,
                previous.max_replans,
            ):
                raise ValueError("plan limits changed mid-turn")
            replanned = is_replan(previous, current.steps)
            if replanned and not current.reason.strip():
                raise ValueError("replanning requires reason")
            expected = previous.replan_count + int(replanned)
        else:
            expected = 0
        if current.replan_count != expected:
            raise ValueError("invalid replan count")
        self.versions.setdefault(current.turn_id, []).append(current)

    def report(
        self, turns: dict[str, dict[str, Any]], turn_id: str | None = None
    ) -> PlanReport | None:
        """默认查询当前会话最后一份计划；终态按实际轮次投影，保留原步骤状态。"""
        if turn_id is None:
            turn_id = next(reversed(self.versions), None)
        history = self.versions.get(turn_id or "", [])
        if not history:
            return None
        turn = turns[history[-1].turn_id]
        return PlanReport(history[-1], turn["status"], turn.get("reason"))

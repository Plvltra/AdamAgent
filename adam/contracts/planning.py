"""显式任务计划的数据契约；完成状态与轮次回答是否保存分别表达。"""

import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

STEP_STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
CLOSED_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class PlanStep:
    """模型维护的稳定步骤；evidence 引用本轮工具调用 ID，不接收模型自造结果。"""

    step_id: str
    content: str
    status: str
    evidence: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """生成 JSON 可用的独立值，证据使用数组。"""
        return {**asdict(self), "evidence": list(self.evidence)}


@dataclass(frozen=True)
class PlanSnapshot:
    """一次完整计划更新；旧版本保留，限制固定于首次建立时。"""

    turn_id: str
    request_id: str
    tool_call_id: str
    version: int
    steps: tuple[PlanStep, ...]
    reason: str = ""
    replan_count: int = 0
    max_steps: int = 8
    max_replans: int = 3

    @property
    def unfinished(self) -> tuple[PlanStep, ...]:
        """返回未闭合步骤；失败和取消已闭合，但不等同于成功。"""
        return tuple(step for step in self.steps if step.status not in CLOSED_STATUSES)

    def to_dict(self) -> dict[str, Any]:
        """保留来源、限制及完整条目，供日志记录和展示使用。"""
        return {**asdict(self), "steps": [step.to_dict() for step in self.steps]}


@dataclass(frozen=True)
class PlanReport:
    """最近计划及其轮次终态；轮次成功只表示回答落盘，不替计划打钩。"""

    snapshot: PlanSnapshot
    turn_status: str
    turn_reason: str | None = None


class TaskPlanner(Protocol):
    """主轮次的计划入口；界面、模型工具和存储实现通过契约解耦。"""

    def begin(self, session_id: str, turn_id: str) -> None:
        """开始新的用户轮次，不继承旧轮次的待办状态。"""
        ...

    def bind_source(self, link: dict[str, str]) -> None:
        """接收执行器生成的来源，模型不能自行指定轮次或请求。"""
        ...

    def update(self, steps: object, reason: str = "") -> dict[str, Any]:
        """保存整份清单，返回已落盘快照；非法模型输入作为工具错误返回。"""
        ...


def decode_steps(value: object) -> tuple[PlanStep, ...]:
    """校验模型或日志的步骤结构；不访问工具记录，错误由调用边界转换。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError("steps must contain 1..64 entries")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "step_id",
            "content",
            "status",
            "evidence",
            "reason",
        }:
            raise ValueError("invalid step fields")
        step_id, content, status = (item.get(key) for key in ("step_id", "content", "status"))
        if not isinstance(step_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", step_id):
            raise ValueError("invalid step id")
        if not isinstance(content, str) or not content.strip() or len(content) > 200:
            raise ValueError("invalid step content")
        if not isinstance(status, str) or status not in STEP_STATUSES:
            raise ValueError("invalid step status")
        evidence, reason = item.get("evidence", []), item.get("reason", "")
        if (
            not isinstance(evidence, list)
            or len(evidence) > 16
            or any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in evidence)
            or len(set(evidence)) != len(evidence)
        ):
            raise ValueError("invalid evidence references")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("invalid step reason")
        if status == "completed" and not evidence:
            raise ValueError("completed step requires tool evidence")
        if status in {"failed", "cancelled"} and not reason.strip():
            raise ValueError("failed/cancelled step requires reason")
        if any(ord(char) < 32 for char in content + reason):
            raise ValueError("step text must be single-line")
        result.append(PlanStep(step_id, content, status, tuple(evidence), reason))
    if len({step.step_id for step in result}) != len(result):
        raise ValueError("duplicate step ids")
    if sum(step.status == "in_progress" for step in result) > 1:
        raise ValueError("only one step may be in progress")
    return tuple(result)


def decode_plan(value: dict[str, Any]) -> PlanSnapshot:
    """解码完整日志快照；拒绝未知字段、布尔计数和超出持久化限制的条目。"""
    fields = {
        "turn_id",
        "request_id",
        "tool_call_id",
        "version",
        "steps",
        "reason",
        "replan_count",
        "max_steps",
        "max_replans",
    }
    if set(value) != fields:
        raise ValueError("invalid plan fields")
    for key in ("turn_id", "request_id", "tool_call_id"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 128:
            raise ValueError("invalid plan source")
    for key, lower, upper in (
        ("version", 1, 10000),
        ("max_steps", 1, 64),
        ("max_replans", 0, 100),
        ("replan_count", 0, 100),
    ):
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise ValueError("invalid plan count")
    if not isinstance(value["reason"], str) or len(value["reason"]) > 500:
        raise ValueError("invalid plan reason")
    if any(ord(char) < 32 for char in value["reason"]):
        raise ValueError("plan reason must be single-line")
    plan = PlanSnapshot(**{**value, "steps": decode_steps(value["steps"])})
    if len(plan.steps) > plan.max_steps or plan.replan_count > plan.max_replans:
        raise ValueError("plan limit exceeded")
    return plan


def is_replan(previous: PlanSnapshot, steps: tuple[PlanStep, ...]) -> bool:
    """内容、顺序或成员变化算重规划；重开已结束步骤也须说明原因并占次数。"""
    if [(step.step_id, step.content) for step in previous.steps] != [
        (step.step_id, step.content) for step in steps
    ]:
        return True
    old = {step.step_id: step for step in previous.steps}
    return any(
        old[step.step_id].status in CLOSED_STATUSES and step.status != old[step.step_id].status
        for step in steps
    )

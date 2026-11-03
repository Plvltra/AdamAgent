"""预算感知委派的纯决策；建议不预留额度，也不替代实际请求的硬检查。"""

from dataclasses import dataclass
from typing import Literal

from adam.contracts.runtime import TokenState


@dataclass(frozen=True)
class DelegationDecision:
    """一次宿主状态快照上的选择；children只表示可尝试派发的数量。"""

    action: Literal["self", "delegate", "collect", "wait", "stop"]
    children: int
    reason: str


def choose_delegation(
    tokens: TokenState,
    *,
    independent_tasks: int,
    active: int,
    uncollected: int,
    free_creation_slots: int,
    max_concurrent: int,
    child_estimate: int,
    main_estimate: int,
) -> DelegationDecision:
    """优先回收结果；只在独立子任务与主任务都留有估算额度时建议委派。

    independent_tasks来自宿主明确拆分，不根据自然语言长度猜复杂度。
    估算不保证未来实际用量；未知用量不按零处理，实际派发须再次检查共享账本。
    """
    values = (
        independent_tasks,
        active,
        uncollected,
        free_creation_slots,
        max_concurrent,
        child_estimate,
        main_estimate,
    )
    if any(type(value) is not int or value < 0 for value in values) or not (
        1 <= independent_tasks <= 32
        and max_concurrent > 0
        and child_estimate > 0
        and main_estimate > 0
    ):
        raise ValueError("invalid bounded delegation input")
    if uncollected:
        return DelegationDecision("collect", 0, "已有未收集结果，先回收证据与用量，避免重复派发。")
    remaining = tokens.remaining
    if remaining is None:
        return DelegationDecision("stop", 0, "用量或预留未知，不能假定还有可用额度。")
    if remaining < main_estimate:
        return DelegationDecision(
            "wait" if active else "stop",
            0,
            "当前余量不足以保留主任务请求；有在途任务时先等待结算。",
        )
    if independent_tasks == 1:
        return DelegationDecision("self", 0, "没有独立并行子任务，主Agent直接处理。")
    slots = min(free_creation_slots, max(0, max_concurrent - active))
    if slots == 0:
        return DelegationDecision(
            "wait" if active else "self", 0, "并发或本轮创建名额已满，不增加隐式队列。"
        )
    count = min(independent_tasks - 1, slots, (remaining - main_estimate) // child_estimate)
    if not count:
        return DelegationDecision("self", 0, "保留主任务额度后不足以新增子请求，由主Agent处理。")
    return DelegationDecision(
        "delegate", count, "存在独立子任务且估算额度足够；保留主任务余量后有界派发。"
    )

"""与 token 预留并用的多单位共享额度；快照替换，未知消费停止新增请求。"""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any

from adam.contracts.consumption import ConsumptionSnapshot, Quantities, sum_snapshots
from adam.contracts.errors import BudgetError, FailureCode


class UnitBudget:
    """每成员只刷新自己的累计消费；预留防止并发重复花费，不能限制服务端实际结算。"""

    def __init__(self, limits: Quantities, *, reserved: Quantities | None = None) -> None:
        self.limits = limits
        self._members: dict[str, ConsumptionSnapshot] = {}
        self._reserved = reserved or Quantities()
        if any(value is None for value in self._reserved.as_record().values()):
            raise ValueError("unknown existing unit reservation")
        self._lock = RLock()

    def update(self, owner: str, usage: ConsumptionSnapshot) -> None:
        """替换成员累计快照；重复刷新不重复结算，损坏读取由调用方标为未知。"""
        with self._lock:
            self._members[owner] = usage

    def usage(self) -> ConsumptionSnapshot:
        """在同一锁内读取各成员已知小计和未知单位，不把在途预留当实际消费。"""
        with self._lock:
            return sum_snapshots(tuple(self._members.values()))

    def check(self) -> None:
        """有未知单位或已发生超额时停止；恰好耗尽某单位仍可执行不消费它的请求。"""
        with self._lock:
            usage = self.usage()
            if usage.unknown_units:
                raise BudgetError(
                    "主会话或子任务存在未知单位消费，未新增请求。", code=FailureCode.USAGE_UNKNOWN
                )
            for unit, maximum in self.limits.as_record().items():
                known = usage.known.as_record()[unit]
                assert known is not None
                if maximum is not None and known > maximum:
                    raise BudgetError("主会话共享单位额度已超限，未新增请求。")

    @contextmanager
    def reserve(self, estimate: Quantities) -> Iterator[None]:
        """在同一锁内检查并占用所有单位；任一不足时不占用，退出总是释放预留。"""
        values = {unit: count for unit, count in estimate.as_record().items() if count is not None}
        if len(values) != len(estimate.as_record()):
            raise BudgetError(
                "请求单位预留包含未知数量，未发送请求。", code=FailureCode.USAGE_UNKNOWN
            )
        with self._lock:
            self.check()
            usage = {
                unit: count
                for unit, count in self.usage().known.as_record().items()
                if count is not None
            }
            reserved = {
                unit: count
                for unit, count in self._reserved.as_record().items()
                if count is not None
            }
            next_reserved = {}
            for unit, count in values.items():
                assert unit in usage and unit in reserved
                maximum = self.limits.as_record()[unit]
                next_reserved[unit] = reserved[unit] + count
                if maximum is not None and usage[unit] + next_reserved[unit] > maximum:
                    raise BudgetError("剩余共享单位额度不足以预留本次请求，未发送请求。")
            self._reserved = Quantities(**next_reserved)
        try:
            yield
        finally:
            with self._lock:
                reserved = {
                    unit: count
                    for unit, count in self._reserved.as_record().items()
                    if count is not None
                }
                self._reserved = Quantities(
                    **{unit: reserved[unit] - count for unit, count in values.items()}
                )

    def as_record(self) -> dict[str, Any]:
        """复制决策资料供录制，不暴露可修改的成员字典。"""
        with self._lock:
            return {
                "members": [
                    {"owner": owner, **usage.as_record()} for owner, usage in self._members.items()
                ],
                "reserved": self._reserved.as_record(),
            }

    @classmethod
    def from_record(cls, limits: Quantities, value: object) -> "UnitBudget":
        """从冻结决策资料恢复其他成员/在途预留；不接受重复所有者或未知预留。"""
        if (
            not isinstance(value, dict)
            or value.keys() != {"members", "reserved"}
            or not isinstance(value["members"], list)
        ):
            raise ValueError("invalid unit budget snapshot")
        result = cls(limits, reserved=Quantities.from_record(value["reserved"]))
        for member in value["members"]:
            if (
                not isinstance(member, dict)
                or member.keys() != {"owner", "known", "unknown_units"}
                or not isinstance(member["owner"], str)
                or not member["owner"]
                or member["owner"] in result._members
            ):
                raise ValueError("invalid unit budget member")
            result.update(
                member["owner"],
                ConsumptionSnapshot.from_record(
                    {key: item for key, item in member.items() if key != "owner"}
                ),
            )
        return result

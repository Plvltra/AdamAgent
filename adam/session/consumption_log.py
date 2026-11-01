"""在既有请求/摘要事件上投影独立单位与配置估价；无第二份消费文件。"""

from decimal import Decimal, localcontext
from typing import Any

from adam.contracts.consumption import (
    UNITS,
    ConsumptionPlan,
    ConsumptionSnapshot,
    Quantities,
    decimal_text,
    quote,
    sum_snapshots,
)


class ConsumptionLog:
    """旧记录没有媒体单位时保持未知；同一请求只开始/结算一次，重读重新构建。"""

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], ConsumptionPlan | None] = {}
        self._usage: dict[tuple[str, str], Quantities] = {}

    def apply(self, kind: str, payload: dict[str, Any]) -> None:
        """原日志关联校验后再接纳计量；可选字段存在但损坏时拒绝整个追加块。"""
        if kind not in {
            "request_started",
            "request_usage",
            "compaction_started",
            "compaction_usage",
        }:
            return
        category = kind.split("_", 1)[0]
        identity = payload["request_id" if category == "request" else "operation_id"]
        key = (category, identity)
        if kind.endswith("_started"):
            if key in self._plans:
                raise ValueError("duplicate consumption start")
            self._plans[key] = (
                ConsumptionPlan.from_record(payload["consumption"])
                if "consumption" in payload
                else None
            )
            return
        if key not in self._plans or key in self._usage:
            raise ValueError("duplicate or unknown consumption settlement")
        plan = self._plans[key]
        if plan is not None:
            amounts = plan.validate_settlement(
                payload.get("consumption"), (payload["input_tokens"], payload["output_tokens"])
            )
        else:
            if "consumption" in payload:
                raise ValueError("consumption settlement without frozen plan")
            # 旧 token 仍能核对；缺少图片/音频计量不能据文字响应倒推为零。
            amounts = Quantities(payload["input_tokens"], payload["output_tokens"], None, None, 1)
        self._usage[key] = amounts

    def snapshot(self) -> ConsumptionSnapshot:
        """返回全日志已知小计；崩溃遗留请求不因恢复终态而得到伪零用量。"""
        parts = []
        for key in self._plans:
            amounts = self._usage.get(key, Quantities(None, None, None, None, None))
            counts = amounts.as_record()
            parts.append(
                ConsumptionSnapshot(
                    Quantities(**{unit: count or 0 for unit, count in counts.items()}),
                    tuple(unit for unit in UNITS if counts[unit] is None),
                )
            )
        return sum_snapshots(tuple(parts))

    def report(self) -> dict[str, Any]:
        """费用按币种给出已知小计和完整性，不跨币种相加或把无价请求当免费。"""
        currencies: dict[str, dict[str, Any]] = {}
        unpriced = 0
        for key, plan in self._plans.items():
            price = plan.price if plan else None
            amounts = self._usage.get(key, Quantities(None, None, None, None, None))
            charge = quote(amounts, price)
            if price is None:
                unpriced += 1
                continue
            entry = currencies.setdefault(price.currency, {"known_cost": "0", "unknown": False})
            with localcontext() as context:
                context.prec = 80
                entry["known_cost"] = decimal_text(
                    Decimal(entry["known_cost"]) + Decimal(charge["known_cost"])
                )
            entry["unknown"] |= charge["cost"] is None
        return {**self.snapshot().as_record(), "costs": currencies, "unpriced_requests": unpriced}

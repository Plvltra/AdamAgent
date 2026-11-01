"""各自计量的请求消费与显式价格；未知不补零，不把配置估价当成服务商账单。"""

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Any

UNITS = ("input_tokens", "output_tokens", "images", "audio_ms", "calls")
FORMAT = "consumption_v1"
MAX_QUANTITY = 10**18
PURPOSES = frozenset(
    {
        "completion",
        "summary",
        "transcription",
        "speech",
        "image_generation",
        "audio_generation",
        "frame_analysis",
    }
)


@dataclass(frozen=True)
class Quantities:
    """一次请求的独立单位；None 是未知，音频单位是毫秒，不能按 token 推算。

    calls 计物理适配器尝试，不证明远端接收或可计费；图片/音频是提交量，不是识别质量。
    """

    input_tokens: int | None = 0
    output_tokens: int | None = 0
    images: int | None = 0
    audio_ms: int | None = 0
    calls: int | None = 0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid consumption quantity")

    def as_record(self) -> dict[str, int | None]:
        """输出全部单位，显式区分零和未知。"""
        return asdict(self)

    @classmethod
    def from_record(cls, value: object) -> "Quantities":
        """拒绝缺失/额外单位；旧记录未知由投影边界处理，不在这里补零。"""
        if not isinstance(value, dict) or value.keys() != set(UNITS):
            raise ValueError("invalid consumption units")
        return cls(**value)


def decimal_text(value: Decimal) -> str:
    """固定小数表示避免指数或浮点舍入；零保持为字符串 0。"""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class RateCard:
    """宿主明确配置的单单位价格快照；没有配置的单位保持未知，不采用模型报价。"""

    currency: str
    per_unit: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise ValueError("invalid consumption currency")
        if not isinstance(self.per_unit, tuple) or not 1 <= len(self.per_unit) <= len(UNITS):
            raise ValueError("invalid consumption rates")
        seen = set()
        for pair in self.per_unit:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid consumption rate")
            unit, text = pair
            if (
                unit not in UNITS
                or unit in seen
                or not isinstance(text, str)
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,18})?", text) is None
            ):
                raise ValueError("invalid consumption rate")
            seen.add(unit)

    def as_record(self) -> dict[str, Any]:
        """冻结明确配置的币种与逐单位字符串单价，不取运行时模型响应。"""
        return {"currency": self.currency, "per_unit": dict(self.per_unit)}

    @classmethod
    def from_record(cls, value: object) -> "RateCard | None":
        """无价格为 None；存在但损坏的价格拒绝读取。"""
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or value.keys() != {"currency", "per_unit"}
            or not isinstance(value["per_unit"], dict)
        ):
            raise ValueError("invalid consumption price")
        return cls(value["currency"], tuple(value["per_unit"].items()))


def quote(amounts: Quantities, rates: RateCard | None) -> dict[str, Any]:
    """按冻结单价精确计算估价；未知数量/缺价列出缺项，跨币种不在此相加。"""
    if rates is None:
        return {"status": "unpriced", "currency": None, "cost": None, "known_cost": None}
    if any(count is not None and count > MAX_QUANTITY for count in amounts.as_record().values()):
        raise ValueError("single priced request exceeds bounded quantity range")
    prices = dict(rates.per_unit)
    missing = []
    with localcontext() as context:
        # 每项至多 19 位数量、12 位整数价格和 18 位小数，保留完整精度后再求和。
        context.prec = 80
        known = Decimal(0)
        for unit, count in amounts.as_record().items():
            if count == 0:
                continue
            if count is None or unit not in prices:
                missing.append(unit)
            else:
                known += count * Decimal(prices[unit])
        subtotal = decimal_text(known)
    return {
        "status": "partial" if missing else "estimated",
        "currency": rates.currency,
        "cost": None if missing else subtotal,
        "known_cost": subtotal,
        "missing_units": missing,
    }


@dataclass(frozen=True)
class ConsumptionPlan:
    """请求前冻结单位预留与可选单价；预留不是供应商消费或费用硬上限。"""

    reservation: Quantities
    purpose: str = "completion"
    price: RateCard | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reservation, Quantities)
            or any(
                count is not None and count > MAX_QUANTITY
                for count in self.reservation.as_record().values()
            )
            or self.reservation.calls != 1
            or not isinstance(self.purpose, str)
            or self.purpose not in PURPOSES
            or (self.price is not None and not isinstance(self.price, RateCard))
        ):
            raise ValueError("invalid consumption plan")

    def as_record(self) -> dict[str, Any]:
        """输出带版本的完整预留与价格快照，供请求开始事件保存。"""
        return {
            "format": FORMAT,
            "purpose": self.purpose,
            "reservation": self.reservation.as_record(),
            "price": self.price.as_record() if self.price is not None else None,
        }

    def settlement(self, amounts: Quantities) -> dict[str, Any]:
        """结算实际已知单位，费用只由此计划中的单价推导，不接收外部声称的费用。"""
        if amounts.calls != 1 or any(
            count is not None and count > MAX_QUANTITY for count in amounts.as_record().values()
        ):
            raise ValueError("one physical invocation must settle one call")
        return {"amounts": amounts.as_record(), "charge": quote(amounts, self.price)}

    @classmethod
    def from_record(cls, value: object) -> "ConsumptionPlan":
        """仅接受已知版本的完整计划，不能忽略损坏单价或隐含单位。"""
        if (
            not isinstance(value, dict)
            or value.keys() != {"format", "purpose", "reservation", "price"}
            or value["format"] != FORMAT
        ):
            raise ValueError("invalid consumption plan record")
        return cls(
            Quantities.from_record(value["reservation"]),
            value["purpose"],
            RateCard.from_record(value["price"]),
        )

    def validate_settlement(
        self, value: object, tokens: tuple[int | None, int | None]
    ) -> Quantities:
        """验证估价及原 token 用量一致；超预留消费允许留证，准入器另行停止后续请求。"""
        if not isinstance(value, dict) or value.keys() != {"amounts", "charge"}:
            raise ValueError("invalid consumption settlement")
        amounts = Quantities.from_record(value["amounts"])
        if (amounts.input_tokens, amounts.output_tokens) != tokens or value != self.settlement(
            amounts
        ):
            raise ValueError("consumption differs from reliable token usage or frozen price")
        return amounts


@dataclass(frozen=True)
class ConsumptionSnapshot:
    """累计已知小计与未知单位；任何未知都保留，不能把小计冒充完整消费。"""

    known: Quantities
    unknown_units: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.known, Quantities)
            or any(value is None for value in self.known.as_record().values())
            or not isinstance(self.unknown_units, tuple)
            or any(unit not in UNITS for unit in self.unknown_units)
            or len(set(self.unknown_units)) != len(self.unknown_units)
        ):
            raise ValueError("invalid consumption snapshot")

    def as_record(self) -> dict[str, Any]:
        """分别保存已知小计和未知标记，方便共享账本替换成员快照。"""
        return {"known": self.known.as_record(), "unknown_units": list(self.unknown_units)}

    @classmethod
    def from_record(cls, value: object) -> "ConsumptionSnapshot":
        """校验完整快照；未知列表缺失时拒绝，不按完整消费处理。"""
        if (
            not isinstance(value, dict)
            or value.keys() != {"known", "unknown_units"}
            or not isinstance(value["unknown_units"], list)
        ):
            raise ValueError("invalid consumption snapshot record")
        return cls(Quantities.from_record(value["known"]), tuple(value["unknown_units"]))


def sum_snapshots(values: tuple[ConsumptionSnapshot, ...]) -> ConsumptionSnapshot:
    """相加独立成员快照；调用方替换同一成员的旧快照，不能重复传入累计值。"""
    totals = {unit: 0 for unit in UNITS}
    unknown = set()
    for value in values:
        for unit, count in value.known.as_record().items():
            assert count is not None
            totals[unit] += count
        unknown.update(value.unknown_units)
    return ConsumptionSnapshot(
        Quantities(**totals), tuple(unit for unit in UNITS if unit in unknown)
    )

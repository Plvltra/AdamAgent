"""宿主显式开启的多单位额度与逐模型估价；缺省保持原 token 流程。"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adam.contracts.consumption import MAX_QUANTITY, Quantities, RateCard
from adam.contracts.errors import ConfigError


@dataclass(frozen=True)
class ConsumptionConfig:
    """图片数、音频毫秒和物理调用数分别设限；价格不提供账单或货币转换保证。"""

    enabled: bool = False
    max_images: int | None = None
    max_audio_ms: int | None = None
    max_calls: int | None = None
    prices: tuple[tuple[str, RateCard], ...] = ()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("consumption.enabled 必须是布尔值。")
        for name in ("max_images", "max_audio_ms", "max_calls"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_QUANTITY):
                raise ConfigError(f"consumption.{name} 必须是有界非负整数；未配置才表示不限。")
        if not isinstance(self.prices, tuple) or len(self.prices) > 64:
            raise ConfigError("consumption.prices 必须是不超过 64 个模型的显式单价。")
        seen = set()
        for pair in self.prices:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ConfigError("consumption.prices 条目无效。")
            model, rate = pair
            if (
                not isinstance(model, str)
                or not model.strip()
                or len(model) > 200
                or model in seen
                or not isinstance(rate, RateCard)
            ):
                raise ConfigError("consumption.prices 模型或单价无效。")
            seen.add(model)
        if not self.enabled and (
            self.prices
            or any(
                getattr(self, name) is not None
                for name in ("max_images", "max_audio_ms", "max_calls")
            )
        ):
            raise ConfigError("配置消费上限或单价时必须显式启用 consumption。")

    def limits(self) -> Quantities:
        """token 仍受原共享额度控制；其余单位独立设限，零额度有效。"""
        return Quantities(None, None, self.max_images, self.max_audio_ms, self.max_calls)

    def price(self, model: str) -> RateCard | None:
        """只精确匹配宿主配置的模型，不猜别名或采用模型响应中的报价。"""
        return dict(self.prices).get(model)

    def as_record(self) -> dict[str, Any]:
        """冻结可重放配置；单价仍为十进制字符串，不包含密钥。"""
        return {
            "enabled": self.enabled,
            "max_images": self.max_images,
            "max_audio_ms": self.max_audio_ms,
            "max_calls": self.max_calls,
            "prices": {model: price.as_record() for model, price in self.prices},
        }


def parse_consumption(value: object) -> ConsumptionConfig:
    """TOML 与录制共用严格校验；无配置为禁用，错误不回显价格原文。"""
    allowed = {"enabled", "max_images", "max_audio_ms", "max_calls", "prices"}
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ConfigError("consumption 存在未知字段或不是配置表。")
    raw = value.get("prices", {})
    if not isinstance(raw, dict) or len(raw) > 64:
        raise ConfigError("consumption.prices 必须是不超过 64 个模型的配置表。")
    try:
        prices = []
        for model, raw_rate in raw.items():
            rate = RateCard.from_record(raw_rate)
            if rate is None:
                raise ValueError("missing price")
            prices.append((model, rate))
        return ConsumptionConfig(
            **{key: item for key, item in value.items() if key != "prices"}, prices=tuple(prices)
        )
    except (TypeError, ValueError):
        raise ConfigError("consumption 单位、币种或十进制字符串单价无效。") from None


DEFAULT_CONSUMPTION = ConsumptionConfig()


def load_consumption_config(path: Path) -> ConsumptionConfig:
    """仅加载独立 consumption 表，不读取聊天配置或 API 密钥；相对路径由调用方决定。"""
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("无法读取消费配置，请检查 UTF-8 TOML 文件。") from None
    if value.keys() != {"consumption"}:
        raise ConfigError("独立消费配置只能包含 [consumption] 表。")
    return parse_consumption(value["consumption"])

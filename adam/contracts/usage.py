"""模型调用的只读观测契约；缓存计数不替代总用量，输入字节不冒充服务端 token。"""

import re
from dataclasses import asdict, dataclass, fields
from typing import Any

CACHE_FORMAT = "chat_completions_cache_v1"
INPUT_LAYOUT_VERSION = "chat_completions_input_v1"


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class CacheUsage:
    """服务端实际报告的输入缓存计数；缺失项为 None，不能由输入总量倒算。"""

    hit_tokens: int | None
    miss_tokens: int | None

    def __post_init__(self) -> None:
        if self.hit_tokens is None and self.miss_tokens is None:
            raise ValueError("empty cache observation")
        if any(v is not None and not _nonnegative(v) for v in (self.hit_tokens, self.miss_tokens)):
            raise ValueError("invalid cache count")

    def check_total(self, input_tokens: int | None) -> None:
        """与已知总输入用量交叉校验；不一致或总输入未知时拒绝认作可靠缓存数据。"""
        if not _nonnegative(input_tokens):
            raise ValueError("cache without reliable input usage")
        assert input_tokens is not None
        counts = (self.hit_tokens, self.miss_tokens)
        if any(value is not None and value > input_tokens for value in counts):
            raise ValueError("cache count exceeds input")
        if (
            all(value is not None for value in counts)
            and sum(v for v in counts if v is not None) != input_tokens
        ):
            raise ValueError("inconsistent cache total")

    def to_record(self) -> dict[str, Any]:
        """生成有格式版本的最小日志字段，不带原始响应或认证信息。"""
        return {"format": CACHE_FORMAT, **asdict(self)}


def decode_cache_usage(value: object, input_tokens: int | None) -> CacheUsage | None:
    """读取本地记录；缺字段兼容为未知，非法记录抛 ValueError，不能当远端缺省吞掉。"""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "hit_tokens", "miss_tokens"}
        or value["format"] != CACHE_FORMAT
    ):
        raise ValueError("invalid cache observation")
    result = CacheUsage(value["hit_tokens"], value["miss_tokens"])
    result.check_total(input_tokens)
    return result


@dataclass(frozen=True)
class InputPrefix:
    """同一客户端相邻输入的逻辑 UTF-8 公共前缀；不表示实际发送或缓存命中。

    scope_id/sequence 定位本客户端的输入准备记录。首次或模型/布局变化时前项为 None；
    日志只保留哈希和长度，不追加原始输入副本。字节布局由 layout_version 解释。
    """

    scope_id: str
    sequence: int
    model: str
    input_bytes: int
    input_sha256: str
    previous_sequence: int | None = None
    previous_bytes: int | None = None
    previous_sha256: str | None = None
    common_prefix_bytes: int | None = None
    layout_version: str = INPUT_LAYOUT_VERSION

    def __post_init__(self) -> None:
        """校验字段、关联与长度的一致性；不能仅因类型是整数就接受布尔值或负值。"""
        if (
            not isinstance(self.scope_id, str)
            or re.fullmatch(r"[0-9a-f]{16}", self.scope_id) is None
            or not _nonnegative(self.sequence)
            or self.sequence < 1
            or not isinstance(self.model, str)
            or not self.model
            or self.layout_version != INPUT_LAYOUT_VERSION
            or not _nonnegative(self.input_bytes)
            or not isinstance(self.input_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.input_sha256) is None
        ):
            raise ValueError("invalid input observation")
        previous = (
            self.previous_sequence,
            self.previous_bytes,
            self.previous_sha256,
            self.common_prefix_bytes,
        )
        if all(value is None for value in previous):
            return
        if (
            not _nonnegative(self.previous_sequence)
            or self.previous_sequence != self.sequence - 1
            or self.previous_sequence == 0
            or not _nonnegative(self.previous_bytes)
            or not isinstance(self.previous_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.previous_sha256) is None
            or not _nonnegative(self.common_prefix_bytes)
        ):
            raise ValueError("invalid previous input")
        assert self.previous_bytes is not None and self.common_prefix_bytes is not None
        if self.common_prefix_bytes > min(self.previous_bytes, self.input_bytes):
            raise ValueError("prefix exceeds input")
        identical = self.input_sha256 == self.previous_sha256
        full_match = self.previous_bytes == self.input_bytes == self.common_prefix_bytes
        if identical != full_match:
            raise ValueError("input hash and prefix disagree")

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def decode_input_prefix(value: object) -> InputPrefix | None:
    """旧记录无观测时返回 None；未知布局、关联损坏和额外字段明确拒绝。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(InputPrefix)}:
        raise ValueError("invalid input observation fields")
    return InputPrefix(**value)


OBSERVATION_FIELDS = frozenset({"cache_usage", "input_prefix"})


def encode_observations(
    cache_usage: CacheUsage | None, input_prefix: InputPrefix | None
) -> dict[str, Any]:
    """只编码实际取得的观测；缺省字段省略，保留旧日志和录制的未知语义。"""
    return {
        **({"cache_usage": cache_usage.to_record()} if cache_usage is not None else {}),
        **({"input_prefix": input_prefix.to_record()} if input_prefix is not None else {}),
    }


def decode_observations(
    record: dict[str, Any], input_tokens: int | None, *, model: str | None = None
) -> tuple[CacheUsage | None, InputPrefix | None]:
    """验证可选观测与调用来源，不改变记录的其他业务字段；损坏时明确拒绝。"""
    cache = decode_cache_usage(record.get("cache_usage"), input_tokens)
    prefix = decode_input_prefix(record.get("input_prefix"))
    if prefix is not None and model is not None and prefix.model != model:
        raise ValueError("input observation model differs from request")
    return cache, prefix

"""窄 RPC 的有界 JSON 帧；可由 WSL Python 3.8 独立加载，不携带工具实现。"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_FRAME_BYTES = 65536
MAX_CALLS = 16
MAX_RESULT_BYTES = 8000


class RpcProtocolError(ValueError):
    """帧损坏、复杂度或顺序无效；整次编排停止，不能把坏帧作为普通工具错误继续。"""


def encode_frame(value: dict[str, Any], *, limit: int = MAX_FRAME_BYTES) -> bytes:
    """返回一条含换行的 UTF-8 帧；拒绝非 JSON 值、非有限数及复杂度超限。"""
    _validate_tree(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (ValueError, TypeError, UnicodeError) as error:
        raise RpcProtocolError("RPC 值不能编码。") from error
    if len(raw) + 1 > limit:
        raise RpcProtocolError("RPC 帧超过长度限制。")
    return raw + b"\n"


def decode_frame(raw: bytes, *, limit: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    """只接受一行对象；重复键、截断、过大整数和深层值一律拒绝。"""
    if len(raw) > limit or not raw.endswith(b"\n"):
        raise RpcProtocolError("RPC 帧超限或不完整。")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_int=_integer)
        if not isinstance(value, dict):
            raise RpcProtocolError("RPC 帧必须是对象。")
        _validate_tree(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise RpcProtocolError("RPC 帧格式无效。") from error


def validate_call(value: dict[str, Any], expected: int) -> tuple[str, dict[str, Any]]:
    """校验唯一顺序号和工具参数对象；名字是否允许由宿主自己的目录决定。"""
    if (
        set(value) != {"kind", "sequence", "name", "arguments"}
        or value["kind"] != "call"
        or type(value["sequence"]) is not int
        or value["sequence"] != expected
        or not 1 <= expected <= MAX_CALLS
        or not isinstance(value["name"], str)
        or not 1 <= len(value["name"]) <= 128
        or not isinstance(value["arguments"], dict)
    ):
        raise RpcProtocolError("RPC 调用顺序、数量或参数无效。")
    return value["name"], value["arguments"]


def _validate_tree(value: object) -> None:
    """字节限额之外再限制深度与节点，避免大量微小字段占用不可控处理时间。"""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 10000 or depth > 32:
            raise RpcProtocolError("RPC 值过于复杂。")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise RpcProtocolError("RPC 对象键必须是文本。")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is float and not math.isfinite(item):
            raise RpcProtocolError("RPC 不接受非有限数。")
        elif item is not None and type(item) not in (str, bool, int, float):
            raise RpcProtocolError("RPC 不接受自定义对象。")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise RpcProtocolError("RPC 对象包含重复键。")
        result[key] = value
    return result


def _integer(value: str) -> int:
    if len(value) > 64:
        raise RpcProtocolError("RPC 整数过长。")
    return int(value)

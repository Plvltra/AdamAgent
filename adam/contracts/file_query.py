"""文件查询的无状态续读游标；绑定查询与扫描版本，不携带路径授权。"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_VERSION = "fq1"
_HASH = re.compile(r"[0-9a-f]{64}")
_CURSOR = re.compile(r"fq1\.([0-9a-f]{64})\.([0-9a-f]{64})\.([0-9]{1,10})")
MAX_OFFSET = 1_000_000_000


@dataclass(frozen=True)
class QueryCursor:
    """offset 是稳定结果序列中的条目位置；每次续读仍须重新校验权限和实际来源版本。"""

    query: str
    generation: str
    offset: int

    def encode(self) -> str:
        """编码有界游标；不签名，调用者不能把它当作可信路径、授权或已保存快照。"""
        if (
            _HASH.fullmatch(self.query) is None
            or _HASH.fullmatch(self.generation) is None
            or type(self.offset) is not int
            or not 0 <= self.offset <= MAX_OFFSET
        ):
            raise ValueError("invalid file query cursor")
        return f"{_VERSION}.{self.query}.{self.generation}.{self.offset}"


def decode_cursor(value: object, query: str, generation: str | None = None) -> QueryCursor:
    """严格核对参数与来源；省略generation时只解析，调用者须在实际扫描后复核版本。

    来源变化时拒绝续接，不能将两次扫描悄悄合并为一个结果集。
    """
    if not isinstance(value, str) or len(value) > 144:
        raise ValueError("文件查询游标格式无效。")
    matched = _CURSOR.fullmatch(value)
    if matched is None or int(matched[3]) > MAX_OFFSET:
        raise ValueError("文件查询游标格式无效。")
    result = QueryCursor(matched[1], matched[2], int(matched[3]))
    if result.encode() != value:
        raise ValueError("文件查询游标不是规范格式。")
    if result.query != query:
        raise ValueError("游标不属于当前路径、查询或匹配方式，请从第一页重新查询。")
    if generation is not None and result.generation != generation:
        raise ValueError("查询来源已变化，旧游标失效，请从第一页重新查询。")
    return result


def query_digest(value: dict[str, Any]) -> str:
    """对规范化参数或扫描资料生成固定摘要；不把它解释成内容安全性证明。"""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

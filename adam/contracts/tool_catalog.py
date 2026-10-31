"""工具说明的只读快照；可见性只约束模型协议，不代替执行授权。"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability


class ToolLoadingMode(StrEnum):
    """全量说明用于对照；按需模式从空选择开始，再通过发现扩展。"""

    FULL = "full"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class ToolListing:
    """目录条目不包含参数 Schema；分组由注册代码指定，不能推导执行权限。"""

    name: str
    group: str
    summary: str
    capability: ToolCapability


@dataclass(frozen=True)
class ToolExposure:
    """一次请求准备使用的固定说明；宿主必须把同一快照用于发送和调用检查。

    构造快照本身不表示请求已发送。后来的发现不会改变旧快照，
    因此同批 discover 后的工具仍须等待下一次提供完整说明的请求。
    """

    names: tuple[str, ...]
    _schemas: tuple[str, ...]

    def definitions(self) -> list[dict[str, Any]]:
        """返回独立 JSON 副本；调用者改写它不会污染目录或其他请求。"""
        return [json.loads(schema) for schema in self._schemas]

    @property
    def schema_bytes(self) -> int:
        """返回 tools 数组以 ensure_ascii=False、默认分隔符编码的字节数，非 HTTP 线长。"""
        return len(("[" + ", ".join(self._schemas) + "]").encode("utf-8"))

    def require(self, name: str) -> None:
        """未出现在此快照时拒绝；通过后仍须原工具的模式、路径、Hook 和批准检查。"""
        if name not in self.names:
            raise ToolError("本次请求尚未提供该工具的完整说明；请先发现工具，再在后续请求调用。")


def tool_schema_record(definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """记录实际提供的名称及统一 JSON 编码大小；不保存参数正文，也不计作精确 token。"""
    if not isinstance(definitions, list) or any(
        not isinstance(tool, dict)
        or not isinstance(tool.get("function"), dict)
        or "name" not in tool["function"]
        for tool in definitions
    ):
        raise ValueError("invalid tool definitions")
    record = {
        "names": [tool["function"]["name"] for tool in definitions],
        "utf8_bytes": len(
            json.dumps(definitions, ensure_ascii=False, allow_nan=False).encode("utf-8")
        ),
    }
    validate_tool_schema_record(record)
    return record


def validate_tool_schema_record(value: object) -> None:
    """校验可选请求说明记录；旧日志缺少此项时保持未知，不伪造为全量或空目录。"""
    if not isinstance(value, dict) or value.keys() != {"names", "utf8_bytes"}:
        raise ValueError("invalid tool schema record")
    names = value["names"]
    if (
        not isinstance(names, list)
        or any(not isinstance(name, str) or not name for name in names)
        or len(names) != len(set(names))
        or type(value["utf8_bytes"]) is not int
        or value["utf8_bytes"] < 2
    ):
        raise ValueError("invalid tool schema metadata")

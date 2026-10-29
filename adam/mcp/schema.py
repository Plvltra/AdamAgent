"""MCP 工具的完整 JSON Schema 校验；仅允许文档内部引用。"""

import json
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from adam.contracts.errors import ToolError


def validator_for(schema: dict[str, Any]) -> Callable[[object], None]:
    """启动时检查 Schema，调用前校验嵌套参数；禁止引用远程地址触发隐式联网。"""
    if len(json.dumps(schema)) > 65536:
        raise ToolError("MCP Schema 超过大小上限。")
    _check_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise ToolError("MCP 工具 Schema 无效。") from error
    validator = Draft202012Validator(schema)

    def validate(arguments: object) -> None:
        """错误不回显模型参数或服务内部细节；无效调用不进入服务进程。"""
        try:
            valid = isinstance(arguments, dict) and validator.is_valid(arguments)
        except Exception as error:
            raise ToolError("MCP 参数 Schema 无法解析。") from error
        if not valid:
            raise ToolError("MCP 参数不符合工具 JSON Schema。")

    return validate


def _check_refs(value: object) -> None:
    """递归检查引用，不让 Schema 校验器隐式抓取外部文档。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ToolError("MCP Schema 只允许内部引用。")
            _check_refs(item)
    elif isinstance(value, list):
        for item in value:
            _check_refs(item)

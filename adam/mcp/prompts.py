"""MCP 提示模板的有限目录、字符串参数和资料结果；外部角色标签只作为资料。"""

import hashlib
import json
import re
from typing import Any

from adam.contracts.errors import ToolError
from adam.mcp.content import ResultContent


def catalog(items: list[dict[str, Any]], allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    """验证目录和参数声明，仅返回允许模板；缺项、重名或超限均不返回残缺目录。"""
    if len(items) > 64:
        raise ToolError("MCP 提示目录超过64项。")
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        name = _name(item.get("name"))
        if name in found:
            raise ToolError("MCP 提示目录包含重复模板名。")
        arguments = item.get("arguments") or []
        if not isinstance(arguments, list) or len(arguments) > 16:
            raise ToolError("MCP 提示参数声明超过16项或格式无效。")
        parameters = []
        names: set[str] = set()
        for argument in arguments:
            if not isinstance(argument, dict):
                raise ToolError("MCP 提示参数声明格式无效。")
            parameter = _name(argument.get("name"))
            required = argument.get("required")
            if parameter in names or (required is not None and type(required) is not bool):
                raise ToolError("MCP 提示参数重复或 required 无效。")
            names.add(parameter)
            parameters.append(
                {
                    "name": parameter,
                    "title": _description(argument.get("title"), 200),
                    "description": _description(argument.get("description"), 2000),
                    "required": required is True,
                }
            )
        found[name] = {
            "name": name,
            "title": _description(item.get("title"), 200),
            "description": _description(item.get("description"), 2000),
            "arguments": parameters,
        }
    if not set(allowed) <= found.keys():
        raise ToolError("MCP 配置允许的模板未出现在目录中。")
    return [found[name] for name in allowed]


def validate_arguments(
    name: object, arguments: object, allowed: tuple[str, ...], items: list[dict[str, Any]]
) -> dict[str, str]:
    """请求前检查显式名称、必填/未知参数和每值2000字符限制；返回独立参数副本。"""
    if not isinstance(name, str) or name not in allowed:
        raise ToolError("MCP 提示模板未获显式允许。")
    item = next((item for item in items if item["name"] == name), None)
    if item is None or not isinstance(arguments, dict) or len(arguments) > 16:
        raise ToolError("MCP 提示模板未在目录中或参数格式无效。")
    declared = {parameter["name"]: parameter["required"] for parameter in item["arguments"]}
    required = {key for key, value in declared.items() if value}
    if arguments.keys() - declared.keys() or not required <= arguments.keys():
        raise ToolError("MCP 提示参数缺少必填项或包含未知项。")
    if any(not isinstance(value, str) or len(value) > 2000 for value in arguments.values()):
        raise ToolError("MCP 提示参数值必须是最多2000字符的字符串。")
    return dict(arguments)


def prompt_result(
    value: dict[str, Any],
    name: str,
    converter: ResultContent,
) -> dict[str, Any]:
    """返回1～16条有界资料及内容摘要；非文本只保留引用，外部角色不是实际历史。"""
    items = value.get("messages")
    if (
        value.get("result_type", "complete") != "complete"
        or not isinstance(items, list)
        or not 1 <= len(items) <= 16
    ):
        raise ToolError("MCP 提示结果不是完整的1～16条消息。")
    messages = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise ToolError("MCP 提示结果角色无效。")
        content = item.get("content")
        if not isinstance(content, dict):
            raise ToolError("MCP 提示内容格式无效。")
        if content.get("type") != "text":
            messages.append({"role": item["role"], "content": converter.nontext(content, index)})
            continue
        text = converter.text(content.get("text"))
        messages.append({"role": item["role"], "text": text})
    version = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "ok": True,
        "name": name,
        "description": _description(value.get("description"), 2000),
        "messages": messages,
        "size_bytes": converter.size_bytes,
        "version": version,
        "version_source": "content_sha256",
        "usage": "reference_only",
    }


def _name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
        raise ToolError("MCP 模板或参数名格式无效。")
    return value


def _description(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError("MCP 提示元信息格式无效或超过上限。")
    return value

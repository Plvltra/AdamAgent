"""有界 MCP 资源资料；URI 只标识远端内容，版本是宿主对当次内容计算的摘要。"""

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from adam.contracts.errors import ToolError

MAX_RESOURCE_BYTES = 65536


def catalog(items: list[dict[str, Any]], allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    """核对有限目录并仅返回显式允许项；未知、重复或缺失 URI 不扩大读取范围。"""
    if len(items) > 64:
        raise ToolError("MCP 资源目录超过64项。")
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        uri = _text(item.get("uri"), 2048)
        assert uri is not None
        if uri in found:
            raise ToolError("MCP 资源目录包含重复 URI。")
        name = _text(item.get("name"), 200)
        size = item.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise ToolError("MCP 资源声明大小无效。")
        found[uri] = {
            "uri": uri,
            "name": name,
            "title": _text(item.get("title"), 200, optional=True),
            "description": _text(item.get("description"), 2000, optional=True),
            "mime_type": _text(item.get("mime_type"), 200, optional=True),
            "declared_size": size,
        }
    if not set(allowed) <= found.keys():
        raise ToolError("MCP 配置允许的资源未出现在目录中。")
    return [found[uri] for uri in allowed]


def validate_read(uri: object, expected_version: object, allowed: tuple[str, ...]) -> None:
    """在协议请求前拒绝未授权 URI 和无效版本；不把 URI 转成本地路径或网络请求。"""
    if not isinstance(uri, str) or uri not in allowed:
        raise ToolError("MCP 资源 URI 未获显式允许。")
    if expected_version is not None and (
        not isinstance(expected_version, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_version) is None
    ):
        raise ToolError("MCP 资源 expected_version 必须是内容 SHA-256。")


def resource_result(
    value: dict[str, Any],
    uri: str,
    expected_version: str | None,
    binary: Callable[[str, str | None, int], dict[str, Any]],
) -> dict[str, Any]:
    """保留原 URI、类型与有界正文或字节引用；跨 URI、混合正文或版本变化均拒绝。"""
    items = value.get("contents")
    if (
        value.get("result_type", "complete") != "complete"
        or not isinstance(items, list)
        or not 1 <= len(items) <= 8
    ):
        raise ToolError("MCP 资源结果不是完整的1～8项内容。")
    contents = []
    size = 0
    text_size = 0
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or item.get("uri") != uri
            or ("text" in item) == ("blob" in item)
        ):
            raise ToolError("MCP 资源只接收所请求 URI 且正文类型唯一的内容。")
        if "blob" in item:
            reference = binary(item["blob"], item.get("mime_type"), index)
            size += reference["size_bytes"]
            contents.append(
                {
                    "uri": uri,
                    "mime_type": reference["mime_type"],
                    "binary_reference": reference,
                    "content_interpreted": False,
                }
            )
            continue
        text = item.get("text")
        if not isinstance(text, str):
            raise ToolError("MCP 资源文本格式无效。")
        try:
            encoded_size = len(text.encode("utf-8"))
            size += encoded_size
            text_size += encoded_size
        except UnicodeError as error:
            raise ToolError("MCP 资源文本无法编码。") from error
        if text_size > MAX_RESOURCE_BYTES:
            raise ToolError("MCP 资源文本超过64 KiB，未截断返回。")
        contents.append(
            {
                "uri": uri,
                "mime_type": _text(item.get("mime_type"), 200, optional=True),
                "text": text,
            }
        )
    version = hashlib.sha256(
        json.dumps(contents, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    if expected_version is not None and version != expected_version:
        raise ToolError("MCP 资源版本已变化，未返回与指定版本不符的内容。")
    return {
        "ok": True,
        "uri": uri,
        "version": version,
        "version_source": "content_sha256",
        "size_bytes": size,
        "contents": contents,
    }


def _text(value: object, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ToolError("MCP 资源元信息缺失或超过上限。")
    return value

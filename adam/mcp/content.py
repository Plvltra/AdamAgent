"""把 MCP 非文本内容转换为实际字节引用或未读取的来源链接；不解释媒体。"""

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any, Literal

from adam.config.mcp import MCPServerConfig
from adam.contracts.errors import ToolError
from adam.mcp.media_store import MCPMediaStore, MediaSource, PreparedMedia, prepare_media


class ResultContent:
    """一次协议结果的转换器；先验证全部内容，调用 publish 后才允许返回本地引用。"""

    def __init__(
        self,
        config: MCPServerConfig,
        store: MCPMediaStore | None,
        kind: Literal["tool", "resource", "prompt"],
        name: str,
    ) -> None:
        self.config, self.store = config, store
        self.kind: Literal["tool", "resource", "prompt"] = kind
        self.name = name
        # 服务身份包含配置选择的地址或程序，不包含认证值；仅摘要进入媒体引用。
        identity = {
            "alias": config.alias,
            "command": config.command,
            "args": config.args,
            "url": config.url,
        }
        self.server_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self._prepared: list[PreparedMedia] = []
        self._text_bytes = 0
        self.size_bytes = 0

    def text(self, value: object) -> str:
        """累计实际文本字节；普通文本和嵌入文本共用64 KiB上限。"""
        text = _text(value)
        size = len(text.encode("utf-8"))
        self._text_bytes += size
        self.size_bytes += size
        if self._text_bytes > 65536:
            raise ToolError("MCP 文本合计超过64 KiB。")
        return text

    def binary(self, blob: str, mime_type: str | None, part: int) -> dict[str, Any]:
        """准备有来源的字节引用；宿主未注入存储时拒绝，不伪造可读地址或回传 base64。"""
        if self.store is None:
            raise ToolError("宿主未启用 MCP 媒体存储，无法返回可读二进制引用。")
        item = prepare_media(blob, mime_type, self._source(part))
        self._prepared.append(item)
        self.size_bytes += item.reference.size_bytes
        return asdict(item.reference)

    def nontext(self, item: dict[str, Any], part: int) -> dict[str, Any]:
        """保留图片/音频/嵌入资料的引用；resource_link 只描述来源，不跟随 URI 访问。"""
        kind = item.get("type")
        if kind in {"image", "audio"}:
            reference = self.binary(item["data"], item.get("mime_type"), part)
            return {"type": kind, "binary_reference": reference, "content_interpreted": False}
        if kind == "resource_link":
            uri = _uri(item.get("uri"))
            declared_size = item.get("size")
            if declared_size is not None and (type(declared_size) is not int or declared_size < 0):
                raise ToolError("MCP 资源链接声明大小无效。")
            return {
                "type": "resource_link",
                "source": asdict(self._source(part)),
                "uri": uri,
                "name": _metadata(item.get("name"), 200),
                "title": _metadata(item.get("title"), 200),
                "description": _metadata(item.get("description"), 2000),
                "mime_type": _metadata(item.get("mime_type"), 200),
                "declared_size": declared_size,
                "content_loaded": False,
                "read_allowed": uri in self.config.resources,
            }
        if kind == "resource":
            resource = item.get("resource")
            if not isinstance(resource, dict) or ("text" in resource) == ("blob" in resource):
                raise ToolError("MCP 嵌入资源须且仅能包含一种正文。")
            value: dict[str, Any] = {
                "type": "resource",
                "uri": _uri(resource.get("uri")),
                "mime_type": _metadata(resource.get("mime_type"), 200),
            }
            if "blob" in resource:
                value["binary_reference"] = self.binary(
                    resource["blob"], resource.get("mime_type"), part
                )
                value["content_interpreted"] = False
            else:
                value["text"] = self.text(resource["text"])
            return value
        raise ToolError("MCP 返回未支持的内容类型；未返回残缺成功。")

    def tool_result(self, value: dict[str, Any]) -> dict[str, Any]:
        """保留原文本工具结果结构，非文本另带位置与引用；全部验证后由调用者发布。"""
        items = value.get("content")
        if (
            value.get("result_type", "complete") != "complete"
            or not isinstance(items, list)
            or len(items) > 16
        ):
            raise ToolError("MCP 工具结果超过16项或内容格式无效。")
        text, references = [], []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError("MCP 工具内容格式无效。")
            if item.get("type") == "text":
                text.append(self.text(item.get("text")))
            else:
                references.append({"part": index, **self.nontext(item, index)})
        structured = value.get("structured_content")
        try:
            size = len(json.dumps(structured, ensure_ascii=False, allow_nan=False).encode())
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise ToolError("MCP 结构化结果不是有效有界 JSON。") from error
        if size > 65536:
            raise ToolError("MCP 结构化结果超过64 KiB。")
        return {
            "ok": not value.get("is_error", False),
            "content": text,
            "structured_content": structured,
            **({"references": references} if references else {}),
            **({"error": "MCP 服务报告工具失败。"} if value.get("is_error") else {}),
        }

    def publish(self) -> None:
        """在完整协议、版本及限额验证后保存所有字节；失败不返回任何本地引用。"""
        if self._prepared:
            assert self.store is not None
            self.store.save_many(self._prepared)

    def _source(self, part: int) -> MediaSource:
        return MediaSource(self.config.alias, self.server_id, self.kind, self.name, part)


def _uri(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) is None
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ToolError("MCP 资料链接 URI 无效。")
    return value


def _metadata(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError("MCP 内容元信息格式无效或过长。")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ToolError("MCP 文本格式无效。")
    try:
        if len(value.encode("utf-8")) > 65536:
            raise ToolError("MCP 文本超过64 KiB。")
    except UnicodeError as error:
        raise ToolError("MCP 文本不能编码。") from error
    return value

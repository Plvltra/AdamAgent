"""网页快照的固定引用与逐字片段；仅处理值，不访问会话或网络。"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, fields, replace
from datetime import datetime
from typing import Any

from adam.web.models import WebError, WebPage


def page_result(page: WebPage, max_chars: int) -> dict[str, Any]:
    """在完整来源结构内缩短正文并绑定引用；无空间时不交付伪有效来源。"""

    def capture(end: int) -> dict[str, Any]:
        delivered = replace(
            page, text=page.text[:end], truncated=page.truncated or end < len(page.text)
        )
        source = asdict(delivered)
        identity = _identity(source)
        return {
            "ok": True,
            "status": "fetched",
            "source": source,
            "source_id": identity,
            "excerpt": _reference(identity, delivered.text, 0, end),
        }

    return _fit_prefix(capture, start=0, end=len(page.text), max_chars=max_chars)


def validate_page_result(result: object) -> WebPage:
    """验证已保存正文、元数据、来源 ID 与片段范围；不接受搜索摘要或错绑结果。"""
    try:
        if not isinstance(result, dict) or set(result) != {
            "ok",
            "status",
            "source",
            "source_id",
            "excerpt",
        }:
            raise ValueError
        if result["ok"] is not True or result["status"] != "fetched":
            raise ValueError
        source = result["source"]
        if not isinstance(source, dict) or set(source) != {field.name for field in fields(WebPage)}:
            raise ValueError
        for name in (
            "requested_url",
            "final_url",
            "fetched_at",
            "sha256",
            "title",
            "text",
            "content_type",
        ):
            if not isinstance(source[name], str):
                raise ValueError
        if (
            type(source["schema_version"]) is not int
            or source["schema_version"] != 1
            or type(source["byte_size"]) is not int
            or not 0 <= source["byte_size"] <= 10000000
            or type(source["truncated"]) is not bool
            or len(source["text"]) > 1000000
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
            or datetime.fromisoformat(source["fetched_at"]).tzinfo is None
            or not isinstance(source["redirects"], (list, tuple))
            or len(source["redirects"]) > 10
            or any(not isinstance(url, str) for url in source["redirects"])
        ):
            raise ValueError
        if not isinstance(result["excerpt"], dict) or any(
            type(result["excerpt"].get(key)) is not int for key in ("start", "end")
        ):
            raise ValueError
        identity = _identity(source)
        if result["source_id"] != identity or result["excerpt"] != _reference(
            identity, source["text"], 0, len(source["text"])
        ):
            raise ValueError
        return WebPage(**{**source, "redirects": tuple(source["redirects"])})
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        raise WebError(
            "invalid_source", "已保存网页来源损坏、错绑或版本不符，未重新联网。"
        ) from None


def quote_page(
    result: dict[str, Any], *, start: int = 0, count: int = 1000, max_chars: int
) -> dict[str, Any]:
    """在已保存正文中逐字分页；没有保存的尾部不补抓，缩短页时不跳过字符。"""
    page = validate_page_result(result)
    if type(start) is not int or not 0 <= start <= len(page.text):
        raise WebError("invalid_range", "网页片段起点超出已保存正文。")
    if type(count) is not int or not 1 <= count <= 4000:
        raise WebError("invalid_range", "网页片段 count 必须为 1 到 4000。")
    metadata = asdict(page)
    del metadata["text"]

    def excerpt(end: int) -> dict[str, Any]:
        text = page.text[start:end]
        return {
            "ok": True,
            "status": "source_excerpt",
            "source_id": result["source_id"],
            "source": metadata,
            "saved_chars": len(page.text),
            "excerpt": {**_reference(result["source_id"], text, start, end), "text": text},
            "next_offset": end if end < len(page.text) else None,
        }

    return _fit_prefix(
        excerpt, start=start, end=min(start + count, len(page.text)), max_chars=max_chars
    )


def _fit_prefix(
    build: Callable[[int], dict[str, Any]], *, start: int, end: int, max_chars: int
) -> dict[str, Any]:
    """按最终 JSON 大小保留最长非空前缀；原文为空时仍允许返回完整元数据。"""
    output = build(end)
    if len(encode_evidence(output)) <= max_chars:
        return output
    # 转义后长度并非原文字符数，不能把超额 JSON 字符数直接从正文中扣除。
    low, high = start + 1, end - 1
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = build(middle)
        if len(encode_evidence(candidate)) <= max_chars:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    if best is None:
        raise WebError("output_limit", "工具输出上限不足以同时保留完整网页来源与正文片段。")
    return best


def encode_evidence(result: dict[str, Any]) -> str:
    """容量核验与最终输出共用同一编码，来源标识不会被通用预览截断。"""
    return json.dumps(result, ensure_ascii=False)


def _identity(source: dict[str, Any]) -> str:
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "web-" + hashlib.sha256(raw).hexdigest()


def _reference(identity: str, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "citation": f"{identity}:{start}-{end}",
    }

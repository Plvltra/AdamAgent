"""只读校验分享容器与脱敏规则；不会把分享事件转换为可重放录制。"""

import json
import math
import tomllib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from adam.recording.format import RecordingError
from adam.recording.redaction import CATEGORIES, RULES_VERSION, LiteralRedaction

MAX_SHARE_BYTES = 64 * 1024 * 1024
SHARE_FORMAT = "adam_shared_recording_v1"
_COUNT = {"type": "integer", "minimum": 0, "maximum": 10000}
_SUMMARY = {
    "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "source_bytes": {"type": "integer", "minimum": 1, "maximum": 256 * 1024 * 1024},
    "source_complete": {"type": "boolean"},
    "source_torn_tail": {"type": "boolean"},
    "event_count": {"type": "integer", "minimum": 1, "maximum": 100000},
    "replacement_count": _COUNT,
    "counts": {
        "type": "object",
        "properties": {key: _COUNT for key in CATEGORIES},
        "additionalProperties": False,
    },
    "manual_review_required": {"const": True},
    "replayable": {"const": False},
}
_RULES = {
    "version": {"const": RULES_VERSION},
    "literal_count": {"type": "integer", "minimum": 0, "maximum": 128},
    "coverage": {"const": "bounded_patterns_and_explicit_literals_not_all_secrets"},
}
_FINDING = {
    "location": {
        "type": "string",
        "maxLength": 2048,
        "pattern": r"^\$(?:\[\d+\]|\.(?:key|value)\[\d+\])*$",
    },
    "category": {"enum": sorted(CATEGORIES)},
    "replacement": {
        "type": "string",
        "maxLength": 100,
        "pattern": r"^<ADAM_REDACTED_[0-9a-f]{16}_(?:email|credential|address|identifier|text)_[1-9]\d{0,4}>$",
    },
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_VALIDATOR = Draft202012Validator(
    _object(
        {
            "format": {"const": SHARE_FORMAT},
            "purpose": {"const": "human_review_only"},
            "created_at": {"type": "string", "maxLength": 64},
            "summary": _object(_SUMMARY),
            "rules": _object(_RULES),
            "findings": {"type": "array", "maxItems": 10000, "items": _object(_FINDING)},
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100000,
                "items": {"type": "object"},
            },
        }
    )
)


def read_share(path: Path) -> dict[str, Any]:
    """返回有界、结构一致的分享数据；仅核对声明格式，不证明脱敏充分或来源可信。"""
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_SHARE_BYTES + 1)
        if len(raw) > MAX_SHARE_BYTES:
            raise ValueError("share size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        _check_tree(value, [600000])
        _VALIDATOR.validate(value)
        if datetime.fromisoformat(value["created_at"]).utcoffset() is None:
            raise ValueError("share timestamp")
        summary = value["summary"]
        if (
            summary["event_count"] != len(value["events"])
            or summary["replacement_count"] != len(value["findings"])
            or summary["counts"] != dict(Counter(f["category"] for f in value["findings"]))
            or (summary["source_complete"] and summary["source_torn_tail"])
        ):
            raise ValueError("share summary mismatch")
        return value
    except (OSError, ValueError, TypeError, RecursionError, ValidationError) as error:
        # 解析器错误可能包含待审原文，统一受控描述避免把它打印到终端。
        raise RecordingError("分享副本不可读、版本不支持、格式损坏或超过上限。") from error


def load_redaction_rules(path: Path | None) -> tuple[LiteralRedaction, ...]:
    """显式读取最多 64 KiB 私有 TOML；报告只保存规则数量，不复制原值或文件路径。"""
    if path is None:
        return ()
    try:
        with path.open("rb") as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise ValueError("rules size")
        value = tomllib.loads(raw.decode("utf-8"))
        if (
            value.keys() != {"version", "literals"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("rules version")
        items = value["literals"]
        if not isinstance(items, list) or len(items) > 128:
            raise ValueError("rules count")
        result = []
        for item in items:
            if (
                not isinstance(item, dict)
                or "value" not in item
                or item.keys() - {"value", "category"}
            ):
                raise ValueError("rule fields")
            result.append(LiteralRedaction(item["value"], item.get("category", "text")))
        return tuple(result)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise RecordingError("脱敏规则文件不可读、格式无效或超过限制；未导出。") from error


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("duplicate share field")
    return result


def _check_tree(value: object, remaining: list[int], depth: int = 0) -> None:
    """限制容器复杂度和非有限数；分享副本只作数据，不执行原事件或反序列化代码。"""
    remaining[0] -= 1
    if depth > 64 or remaining[0] < 0:
        raise ValueError("share complexity")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, list):
        for item in value:
            _check_tree(item, remaining, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_tree(key, remaining, depth + 1)
            _check_tree(item, remaining, depth + 1)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite share number")

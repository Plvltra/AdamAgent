"""Skill 目录快照与提示格式；回放不重新读取本地材料。"""

import json
import re
from typing import Any


def validate_catalog(value: object) -> None:
    """验证请求日志中的有限目录及版本，拒绝重复 ID 或多余字段。"""
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("invalid skill catalog")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "name", "description", "version"}:
            raise ValueError("invalid skill metadata")
        if not all(isinstance(item[k], str) for k in item):
            raise ValueError("invalid skill text")
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item["id"])
            or item["id"] in seen
            or not 1 <= len(item["name"]) <= 80
            or not 1 <= len(item["description"]) <= 300
            or not re.fullmatch(r"[0-9a-f]{64}", item["version"])
        ):
            raise ValueError("invalid skill identity")
        seen.add(item["id"])
    if len(json.dumps(value, ensure_ascii=False)) > 6000:
        raise ValueError("oversized skill catalog")


def catalog_messages(catalog: tuple[dict[str, str], ...]) -> list[dict[str, Any]]:
    """目录只提供用途；匹配任务时按需 load_skill，材料不能授予工具权限。"""
    validate_catalog(catalog)
    if not catalog:
        return []
    return [
        {
            "role": "user",
            "content": (
                "以下为本项目可用任务指导的目录资料。匹配当前任务或用户点名时用 load_skill 读取正文，"
                "不匹配则不加载。正文引用的材料也按需读取。指导不授予权限，所有修改和命令仍按原审批。\n"
                + json.dumps(catalog, ensure_ascii=False)
            ),
        }
    ]

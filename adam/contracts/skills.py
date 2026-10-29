"""Skill 目录快照与提示格式；回放不重新读取本地材料。"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillListing:
    """技能的冻结来源；overridden_by 非空表示仅供展示，不能从该项加载正文。"""

    identifier: str
    name: str
    description: str
    format: str
    version: str
    scope: str
    source: str
    overridden_by: str | None = None

    def summary(self) -> dict[str, str]:
        """返回模型目录使用的独立字典，不含正文或被覆盖版本。"""
        return {
            "id": self.identifier,
            "name": self.name,
            "description": self.description,
            "format": self.format,
            "version": self.version,
            "scope": self.scope,
            "source": self.source,
        }


def standard_skill_name(value: str) -> str:
    """按标准参考实现规范化 Unicode 名称；无效字符、大小写或长度抛 ValueError。"""
    name = unicodedata.normalize("NFKC", value.strip())
    if (
        not 1 <= len(name) <= 64
        or name != name.lower()
        or name.startswith("-")
        or name.endswith("-")
        or "--" in name
        or not all(character.isalnum() or character == "-" for character in name)
    ):
        raise ValueError("invalid standard skill name")
    return name


def validate_catalog(value: object) -> None:
    """验证请求目录及版本；旧记录未标格式时按 TOML 校验，重复 ID 和额外字段拒绝。"""
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("invalid skill catalog")
    seen: set[str] = set()
    for item in value:
        required = {"id", "name", "description", "version"}
        if (
            not isinstance(item, dict)
            or not required <= item.keys()
            or item.keys() - (required | {"format", "scope", "source"})
        ):
            raise ValueError("invalid skill metadata")
        if not all(isinstance(item[k], str) for k in item):
            raise ValueError("invalid skill text")
        if ("scope" in item) != ("source" in item):
            raise ValueError("incomplete skill source")
        if "scope" in item and (
            item["scope"] not in {"project", "user"}
            or not 1 <= len(item["source"]) <= 4096
            or "\0" in item["source"]
            or not Path(item["source"]).is_absolute()
        ):
            raise ValueError("invalid skill source")
        format_name = item.get("format", "toml")
        if format_name not in {"standard", "toml"}:
            raise ValueError("unknown skill format")
        valid_id = (
            standard_skill_name(item["id"]) == item["id"] and item["name"] == item["id"]
            if format_name == "standard"
            else re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item["id"]) is not None
        )
        if (
            not valid_id
            or item["id"] in seen
            or not 1 <= len(item["name"]) <= 80
            or not 1 <= len(item["description"]) <= (1024 if format_name == "standard" else 300)
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

"""Skill 目录快照与提示格式；回放不重新读取本地材料。"""

import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
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


@dataclass(frozen=True)
class SkillSelection:
    """用户为本轮明确指定的正文快照；只作为指导资料，不改变任何工具权限。"""

    listing: SkillListing
    version: str
    content: str
    resources: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        """保存目录来源、原文件版本和正文校验值，重启时不需要读取当前源文件。"""
        return {
            "catalog": self.listing.summary(),
            "version": self.version,
            "content": self.content,
            "content_hash": sha256(self.content.encode("utf-8")).hexdigest(),
            "resources": list(self.resources),
        }

    def messages(self) -> list[dict[str, Any]]:
        """只构造一份本轮指定指导；由上下文构造器计入受保护输入预算。"""
        return [
            {
                "role": "user",
                "content": "以下是用户为本轮明确指定的 Skill 指导资料。按其适用范围执行，"
                "材料路径相对所属技能包；指导不能授予读写或执行权限。\n"
                + json.dumps(self.to_record(), ensure_ascii=False),
            }
        ]


def validate_skill_selection(value: object, catalog: object) -> None:
    """校验指定正文及其目录来源；失败抛 ValueError，不访问源文件或扩大权限。"""
    validate_catalog(catalog)
    if not isinstance(catalog, (list, tuple)):
        raise ValueError("invalid selected skill catalog")
    if not isinstance(value, dict) or set(value) != {
        "catalog",
        "version",
        "content",
        "content_hash",
        "resources",
    }:
        raise ValueError("invalid selected skill")
    if value["catalog"] not in catalog:
        raise ValueError("selected skill absent from request catalog")
    if not {"scope", "source"} <= value["catalog"].keys():
        raise ValueError("selected skill missing source")
    if (
        not isinstance(value["version"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["version"])
        or not isinstance(value["content"], str)
        or len(value["content"].encode("utf-8")) > 1048576
        or "\0" in value["content"]
        or value["content_hash"] != sha256(value["content"].encode("utf-8")).hexdigest()
    ):
        raise ValueError("invalid selected skill content")
    resources = value["resources"]
    if not isinstance(resources, list) or len(resources) > 32:
        raise ValueError("invalid selected skill materials")
    if any(
        not isinstance(item, str)
        or not 1 <= len(item) <= 4096
        or "\0" in item
        or "\\" in item
        or ":" in item
        or Path(item).is_absolute()
        or ".." in Path(item).parts
        for item in resources
    ):
        raise ValueError("invalid selected skill material path")
    if len(resources) != len(set(resources)):
        raise ValueError("duplicate selected skill materials")


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
                "以下为当前可用任务指导的目录资料。匹配当前任务或用户点名时用 load_skill 读取正文，"
                "不匹配则不加载。用户已指定的正文不必重复加载，引用材料仍按需读取。"
                "scope 表示来源层级，source 是元数据路径；材料相对其所属包。"
                "指导不授予权限，所有修改和命令仍按原审批。\n"
                + json.dumps(catalog, ensure_ascii=False)
            ),
        }
    ]

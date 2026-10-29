"""有界读取标准 SKILL.md 元数据；不读取正文，不执行 YAML 对象或技能声明。"""

import json
from collections.abc import Hashable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, NodeEvent
from yaml.nodes import MappingNode

from adam.contracts.errors import ToolError
from adam.contracts.skills import standard_skill_name


@dataclass(frozen=True)
class SkillHeader:
    """已校验的元数据与原始头部版本；body_offset 是正文在文件中的字节位置。"""

    name: str
    description: str
    version: str
    body_offset: int
    metadata: dict[str, Any]


def read_header(path: Path, identifier: str, *, maximum: int = 8192) -> SkillHeader:
    """只读取两条 --- 之间的 UTF-8 YAML，并返回名称、用途和完整元数据。

    调用者先完成路径授权；本函数限制头部字节、解析深度和事件数。
    不支持重复字段、别名或自定义对象标签；失败抛 ToolError，不省略坏包。
    """
    try:
        with path.open("rb") as stream:
            return _read_header(stream, identifier, maximum)
    except OSError as error:
        raise ToolError("Skill 元数据无法读取，请检查文件和权限。") from error


def header_from_bytes(raw: bytes, identifier: str, *, maximum: int = 8192) -> SkillHeader:
    """从已读取的完整文件解析同一份头部，让正文和版本校验使用相同字节快照。"""
    return _read_header(BytesIO(raw), identifier, maximum)


def _read_header(stream: BinaryIO, identifier: str, maximum: int) -> SkillHeader:
    """解析有界头部，停在结束标记后，不解码或扫描后续正文。"""
    first = stream.readline(maximum + 1)
    raw = bytearray(first)
    if first.removeprefix(b"\xef\xbb\xbf").rstrip(b"\r\n") != b"---":
        raise ToolError("标准 SKILL.md 必须以 YAML frontmatter 的 --- 开始。")
    lines = []
    while len(raw) <= maximum:
        line = stream.readline(maximum - len(raw) + 1)
        if not line:
            raise ToolError("Skill 元数据缺少结束的 ---。")
        raw.extend(line)
        if len(raw) > maximum:
            break
        if line.rstrip(b"\r\n") == b"---":
            try:
                data = _parse(b"".join(lines).decode("utf-8"))
            except UnicodeError as error:
                raise ToolError("Skill 元数据不是有效 UTF-8。") from error
            _validate(data, identifier)
            return SkillHeader(
                data["name"],
                data["description"].strip(),
                sha256(raw).hexdigest(),
                len(raw),
                data,
            )
        lines.append(line)
    raise ToolError("Skill 元数据超过8 KiB。")


def _parse(text: str) -> dict[str, Any]:
    """先检查解析事件再构造安全类型，防止别名扩张、深嵌套及重复键覆盖。"""
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=yaml.SafeLoader), 1):
            if isinstance(event, AliasEvent) or (
                isinstance(event, NodeEvent) and event.anchor is not None
            ):
                raise ToolError("Skill YAML 不支持锚点或别名。")
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if depth > 8 or count > 2048:
                raise ToolError("Skill YAML 嵌套或节点数量超限。")
        data = yaml.load(text, Loader=_UniqueLoader)
        if not isinstance(data, dict):
            raise ToolError("Skill 元数据必须为字段表。")
        # 扩展字段只保留 JSON 数据，不能携带 Python 对象、集合或非有限数值。
        json.dumps(data, ensure_ascii=False, allow_nan=False)
        return data
    except (yaml.YAMLError, ValueError, TypeError) as error:
        raise ToolError("Skill 元数据不是有效的安全 YAML 字段表。") from error


def _validate(data: dict[str, Any], identifier: str) -> None:
    """校验标准核心字段；可选及扩展声明保留为数据，不产生工具授权。"""
    name, description = data.get("name"), data.get("description")
    try:
        if not isinstance(name, str) or standard_skill_name(name) != standard_skill_name(
            identifier
        ):
            raise ValueError("name differs from directory")
        data["name"] = standard_skill_name(name)
    except ValueError as error:
        raise ToolError("Skill name 必须是有效标准 ID，且与包目录名相同。") from error
    if not isinstance(description, str) or (not description.strip() or len(description) > 1024):
        raise ToolError("Skill description 必须是1～1024字符的非空用途说明。")
    for field, limit in (("license", 8192), ("compatibility", 500), ("allowed-tools", 8192)):
        if field in data and (
            not isinstance(data[field], str) or not 1 <= len(data[field].strip()) <= limit
        ):
            raise ToolError(f"Skill {field} 必须是符合长度限制的非空字符串。")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
    ):
        raise ToolError("Skill metadata 必须是字符串键值表。")


class _UniqueLoader(yaml.SafeLoader):
    """仅扩展 SafeLoader 的重复键校验，不注册任何可执行对象构造器。"""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        """各层映射都要求字符串键且不能重复；不接受 YAML merge 覆盖。"""
        if not isinstance(node, MappingNode):
            raise yaml.YAMLError("expected mapping")
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise yaml.YAMLError("non-string or duplicate key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result

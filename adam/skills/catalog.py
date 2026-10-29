"""统一标准与 TOML Skill 目录；元数据冻结，正文与材料按需读取。"""

import json
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.skills import validate_catalog
from adam.skills.discovery import SkillSource, discover_packages
from adam.skills.frontmatter import header_from_bytes, read_header


@dataclass(frozen=True)
class SkillPackage:
    """单包的发现快照；source 保留真实目录，identifier 是模型使用的目录 ID。"""

    source: SkillSource
    identifier: str
    name: str
    description: str
    version: str
    resources: tuple[str, ...]

    def summary(self) -> dict[str, str]:
        """只返回目录元数据；附属材料和正文不进入每次请求。"""
        return {
            "id": self.identifier,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "format": self.source.format,
        }


class SkillCatalog:
    """启动时冻结有界目录；root 可指定独立来源，读取仍必须通过注入的授权函数。

    正文仅在 load 时读取；更新元数据后需要重启应用，不自动扩大普通文件工具权限。
    """

    def __init__(
        self,
        workspace: Path,
        resolve: Callable[[str], Path],
        *,
        root: Path | None = None,
        max_bytes: int = 1048576,
        max_result_chars: int = 8000,
    ) -> None:
        self.root = (
            Path(os.path.abspath(root)) if root is not None else workspace.resolve() / "skills"
        )
        self.resolve = resolve
        self.max_bytes, self.max_result_chars = max_bytes, max_result_chars
        self._entries: dict[str, SkillPackage] = {}
        try:
            self._scan()
        except OSError as error:
            raise ToolError("Skill 目录无法读取，请检查文件与权限。") from error

    def summaries(self) -> tuple[dict[str, str], ...]:
        """返回目录副本；标准包只读头部，旧包只读元数据与格式标记。"""
        return tuple(entry.summary() for entry in self._entries.values())

    def source_path(self, skill_id: str) -> Path:
        """返回冻结的元数据来源路径；不读取文件，未知 ID 抛 ToolError。"""
        entry = self._entries.get(skill_id)
        if entry is None:
            raise ToolError("Skill 未登记。")
        return entry.source.source

    def load(self, skill_id: str, resource: str = "SKILL.md") -> dict[str, Any]:
        """返回完整指导、材料清单与字节版本；格式或元数据变化时要求重启。

        标准包只返回 Markdown 正文，元数据另列为数据；scripts/、references/、assets/
        的文本可以读取但不执行。包外路径、未登记、坏编码或超长材料明确失败，不截断。
        """
        entry = self._entries.get(skill_id)
        if entry is None or resource not in ("SKILL.md", *entry.resources):
            raise ToolError("Skill 或材料未登记；请只使用目录 ID 和明确列出的材料。")
        if entry.source.format == "standard" and (entry.source.directory / "skill.toml").exists():
            raise ToolError("Skill 元数据来源已变化，请重启应用刷新目录。")
        path = self._path(entry.source, resource)
        raw = self._read(path, self.max_bytes)
        metadata: dict[str, Any] | None = None
        content = raw
        if entry.source.format == "standard":
            # 正文与头部使用同一次有界读取，避免检查后文件被替换导致版本与内容不一致。
            header = (
                header_from_bytes(raw, entry.source.identifier)
                if resource == "SKILL.md"
                else read_header(self._path(entry.source, "SKILL.md"), entry.source.identifier)
            )
            current_version = header.version
            metadata = header.metadata
            if resource == "SKILL.md":
                content = raw[header.body_offset :]
        else:
            meta = self._read(self._path(entry.source, "skill.toml"), 8192)
            current_version = sha256(meta).hexdigest()
            if resource == "SKILL.md" and raw.removeprefix(b"\xef\xbb\xbf").splitlines()[0:1] == [
                b"---"
            ]:
                raise ToolError("Skill 元数据来源已变化，请重启应用刷新目录。")
        if current_version != entry.version:
            raise ToolError("Skill 元数据已变化，请重启应用刷新目录。")
        try:
            body = content.decode("utf-8-sig")
        except UnicodeError as error:
            raise ToolError("Skill 材料必须是 UTF-8 文本，暂不读取二进制材料。") from error
        if "\0" in body:
            raise ToolError("Skill 材料必须是文本。")
        result: dict[str, Any] = {
            "skill_id": skill_id,
            "resource": resource,
            "format": entry.source.format,
            "version": sha256(raw).hexdigest(),
            "metadata_version": entry.version,
            "content": body,
            "resources": list(entry.resources),
        }
        if metadata is not None:
            result["metadata"] = metadata
        if len(json.dumps({"ok": True, **result}, ensure_ascii=False)) > self.max_result_chars:
            raise ToolError("Skill 材料超过工具结果上限，请按职责拆成较小的材料后重试。")
        return result

    def _scan(self) -> None:
        """发现唯一来源并校验有界目录；坏包不能被静默省略。"""
        for source in discover_packages(self.root, self.resolve):
            entry = self._standard(source) if source.header else self._legacy(source)
            if entry.identifier in self._entries:
                raise ToolError("Skill 目录 ID 重复，请调整包名称。")
            self._entries[entry.identifier] = entry
        try:
            validate_catalog(self.summaries())
        except ValueError as error:
            raise ToolError("Skill 目录超过16项或6000字符，请精简用途说明。") from error

    def _standard(self, source: SkillSource) -> SkillPackage:
        """冻结标准包的元数据与材料路径，不预读附属材料内容。"""
        header = source.header
        if header is None:
            raise ValueError("missing standard header")
        return SkillPackage(
            source,
            header.name,
            header.name,
            header.description,
            header.version,
            self._materials(source),
        )

    def _legacy(self, source: SkillSource) -> SkillPackage:
        """沿用旧 TOML 字段、长度和 Markdown 清单契约，已有包无需迁移。"""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source.identifier):
            raise ToolError("Skill 目录 ID 必须为小写字母、数字和连字符。")
        raw = self._read(source.source, 8192)
        try:
            data = tomllib.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as error:
            raise ToolError("Skill 元数据不是有效 UTF-8 TOML。") from error
        if set(data) != {"name", "description", "resources"}:
            raise ToolError("skill.toml 必须且只能包含 name、description、resources。")
        for key, limit in (("name", 80), ("description", 300)):
            if not isinstance(data[key], str) or not 1 <= len(data[key].strip()) <= limit:
                raise ToolError("Skill 名称或用途为空或过长。")
            data[key] = data[key].strip()
        resources = data["resources"]
        if (
            not isinstance(resources, list)
            or len(resources) > 8
            or any(not isinstance(r, str) or not self._valid_resource(r) for r in resources)
            or len(set(resources)) != len(resources)
        ):
            raise ToolError("Skill resources 必须是最多8个不重复的包内 Markdown 相对路径。")
        return SkillPackage(
            source,
            source.identifier,
            data["name"],
            data["description"],
            sha256(raw).hexdigest(),
            tuple(resources),
        )

    def _materials(self, source: SkillSource) -> tuple[str, ...]:
        """只登记包内文件路径，最多32份材料、128目录项和8层；链接不能逃出包。"""
        pending = [(source.directory, 0)]
        resources: list[str] = []
        visited: set[Path] = set()
        scanned = 0
        while pending:
            directory, depth = pending.pop()
            if depth > 8 or directory in visited:
                raise ToolError("Skill 材料目录过深或存在循环链接。")
            visited.add(directory)
            for candidate in directory.iterdir():
                scanned += 1
                if scanned > 128:
                    raise ToolError("Skill 材料目录项超过128项。")
                relative = candidate.relative_to(source.directory).as_posix()
                if relative in {"SKILL.md", "skill.toml"} or candidate.name in {
                    ".git",
                    ".venv",
                    "__pycache__",
                }:
                    continue
                resolved = self._path(source, relative)
                if resolved.is_dir():
                    pending.append((resolved, depth + 1))
                elif resolved.is_file():
                    resources.append(relative)
                else:
                    raise ToolError("Skill 材料必须为普通文件。")
                if len(resources) > 32:
                    raise ToolError("标准 Skill 材料超过32份。")
        return tuple(sorted(resources))

    def _path(self, source: SkillSource, resource: str) -> Path:
        """同时检查包内边界和既有读取授权，拒绝软链接/联接逃出包目录。"""
        resolved = self.resolve(str(source.directory / resource))
        if not resolved.is_relative_to(source.directory) or not resolved.is_relative_to(self.root):
            raise ToolError("Skill 材料不能通过链接离开所属目录。")
        return resolved

    @staticmethod
    def _valid_resource(value: str) -> bool:
        path = Path(value)
        return (
            value != "SKILL.md"
            and path.suffix.lower() == ".md"
            and not path.is_absolute()
            and ".." not in path.parts
            and ":" not in value
            and "\\" not in value
        )

    @staticmethod
    def _read(path: Path, maximum: int) -> bytes:
        """先限制字节读取量；错误交给工具/启动边界报告，不使用无界 read_text。"""
        if not path.is_file():
            raise ToolError("Skill 材料不是普通文件。")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ToolError("Skill 文件超过字节上限。")
        return data

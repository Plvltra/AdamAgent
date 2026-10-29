"""读取本地 Skill 元数据及按需材料；元数据与正文分开加载。"""

import json
import re
import tomllib
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.contracts.errors import ToolError


class SkillCatalog:
    """启动时冻结有界目录，正文仅在 load 时读取；更新元数据后需要重启应用。"""

    def __init__(
        self,
        workspace: Path,
        resolve: Callable[[str], Path],
        *,
        max_bytes: int = 1048576,
        max_result_chars: int = 8000,
    ) -> None:
        self.root = workspace.resolve() / "skills"
        self.resolve = resolve
        self.max_bytes, self.max_result_chars = max_bytes, max_result_chars
        self._entries: dict[str, dict[str, Any]] = {}
        if not self.root.exists():
            return
        self._scan()

    def summaries(self) -> tuple[dict[str, str], ...]:
        """返回目录副本；不打开 SKILL.md，不暴露未允许的材料路径。"""
        return tuple(
            {key: entry[key] for key in ("id", "name", "description", "version")}
            for entry in self._entries.values()
        )

    def load(self, skill_id: str, resource: str = "SKILL.md") -> dict[str, Any]:
        """返回完整指导和字节版本；越界、元数据变化、超长材料报错而不截断。"""
        entry = self._entries.get(skill_id)
        if entry is None or resource not in ["SKILL.md", *entry["resources"]]:
            raise ToolError("Skill 或材料未登记；请只使用目录 ID 和明确列出的材料。")
        meta = self._read(self._path(skill_id, "skill.toml"), 8192)
        if sha256(meta).hexdigest() != entry["version"]:
            raise ToolError("Skill 元数据已变化，请重启应用刷新目录。")
        raw = self._read(self._path(skill_id, resource), self.max_bytes)
        try:
            body = raw.decode("utf-8-sig")
        except UnicodeError as error:
            raise ToolError("Skill 材料必须是 UTF-8。") from error
        if "\0" in body:
            raise ToolError("Skill 材料必须是文本。")
        result = {
            "skill_id": skill_id,
            "resource": resource,
            "version": sha256(raw).hexdigest(),
            "content": body,
            "resources": list(entry["resources"]),
        }
        if len(json.dumps({"ok": True, **result}, ensure_ascii=False)) > self.max_result_chars:
            raise ToolError("Skill 材料超过工具结果上限，请按职责拆成较小的材料后重试。")
        return result

    def _scan(self) -> None:
        """只读一层 TOML 目录；无效包拒绝加载，防止默默省略指导。"""
        children = []
        for child in self.root.iterdir():
            children.append(child)
            if len(children) > 32:
                raise ToolError("skills 目录项过多，最多32项。")
        for directory in sorted(children):
            if not directory.is_dir():
                continue
            identifier = directory.name
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identifier):
                raise ToolError("Skill 目录 ID 必须为小写字母、数字和连字符。")
            raw = self._read(self._path(identifier, "skill.toml"), 8192)
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
            self._entries[identifier] = {
                **data,
                "id": identifier,
                "version": sha256(raw).hexdigest(),
            }
        if len(self._entries) > 16 or len(json.dumps(self.summaries(), ensure_ascii=False)) > 6000:
            raise ToolError("Skill 目录超过16项或6000字符，请精简用途说明。")

    def _path(self, skill_id: str, resource: str) -> Path:
        """同时检查包内边界和既有读取授权，拒绝软链接/联接逃出包目录。"""
        package = self.root / skill_id
        resolved = self.resolve(str(package / resource))
        if not resolved.is_relative_to(package) or not resolved.is_relative_to(self.root):
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

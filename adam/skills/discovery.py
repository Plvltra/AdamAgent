"""发现标准与既有 TOML 技能包来源；不混合两种元数据，不加载材料正文。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from adam.contracts.errors import ToolError
from adam.skills.frontmatter import SkillHeader, read_header


@dataclass(frozen=True)
class SkillSource:
    """包目录及唯一元数据来源；TOML 字段仍交给既有解析器处理。"""

    identifier: str
    directory: Path
    source: Path
    format: Literal["standard", "toml"]
    header: SkillHeader | None = None


def discover_packages(root: Path, authorize: Callable[[str], Path]) -> tuple[SkillSource, ...]:
    """按名称发现一层有界目录，返回标准头部或 TOML 来源。

    authorize 沿用调用者的读取权限；路径解析后不得离开原包。
    同包同时出现 TOML 和标准 frontmatter 时拒绝，不猜优先级。
    """
    root = root.absolute()
    if not root.exists():
        return ()
    try:
        if authorize(str(root)) != root or not root.is_dir():
            raise ToolError("Skill 根目录不能通过链接跳转或指向普通文件。")
        directories = []
        for child in root.iterdir():
            directories.append(child)
            if len(directories) > 32:
                raise ToolError("skills 目录项过多，最多32项。")
        result = []
        standard_names: set[str] = set()
        for package in sorted(directories):
            if not package.is_dir():
                continue
            if len(result) >= 16:
                raise ToolError("Skill 包超过16项。")
            legacy = package / "skill.toml"
            if legacy.exists():
                source = _within(package, "skill.toml", authorize)
                guide = package / "SKILL.md"
                if guide.exists():
                    with _within(package, "SKILL.md", authorize).open("rb") as stream:
                        marker = stream.readline(9).removeprefix(b"\xef\xbb\xbf")
                    if marker.rstrip(b"\r\n") == b"---":
                        raise ToolError(
                            "同一 Skill 同时存在 TOML 和标准元数据，请保留一种明确来源。"
                        )
                result.append(SkillSource(package.name, package, source, "toml"))
            else:
                source = _within(package, "SKILL.md", authorize)
                header = read_header(source, package.name)
                if header.name in standard_names:
                    raise ToolError("标准 Skill 名称规范化后重复，请调整目录和名称。")
                standard_names.add(header.name)
                result.append(SkillSource(package.name, package, source, "standard", header))
        return tuple(result)
    except OSError as error:
        raise ToolError("Skill 目录或元数据无法读取，请检查路径和权限。") from error


def _within(package: Path, name: str, authorize: Callable[[str], Path]) -> Path:
    """读取前校验真实文件仍在包内，不通过链接读取其他包或授权根以外的材料。"""
    target = authorize(str(package / name))
    if not target.is_relative_to(package) or not target.is_file():
        raise ToolError("Skill 元数据必须为包内普通文件，不能通过链接离开包目录。")
    return target

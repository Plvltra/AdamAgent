"""沿实际目标的祖先目录读取 AGENTS.md，不递归扫描项目或扩大读取权限。"""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from adam.contracts.errors import ToolError
from adam.contracts.rules import RuleDocument, RuleSelection


class RuleCatalog:
    """每次 select 读取当前版本；不存在的规则可缺省，损坏或无权读取则明确失败。"""

    def __init__(self, workspace: Path, authorize: Callable[[str], Path]) -> None:
        self.workspace = workspace.resolve()
        self.authorize = authorize

    def select(self, targets: tuple[Path, ...] = ()) -> RuleSelection:
        """返回根规则及目标祖先规则，合并重复来源并保持根到叶顺序。

        targets 可以是文件或已有目录；不存在的目标按文件处理。
        额外授权目录的文件不会获得本项目的子目录规则；规则源本身仍逐个授权。
        """
        if len(targets) > 64:
            raise ToolError("本轮项目规则涉及的目标超过64个，请拆分任务。")
        directories = {self.workspace}
        for target in targets:
            resolved = self.authorize(str(target))
            if not resolved.is_relative_to(self.workspace):
                continue
            directory = resolved if resolved.is_dir() else resolved.parent
            depth = len(directory.relative_to(self.workspace).parts)
            if depth > 32:
                raise ToolError("项目规则目录深度超过32层。")
            while directory.is_relative_to(self.workspace):
                directories.add(directory)
                if directory == self.workspace:
                    break
                directory = directory.parent
        documents = []
        total = 0
        ordered = sorted(
            directories,
            key=lambda path: (
                len(path.relative_to(self.workspace).parts),
                path.relative_to(self.workspace).as_posix(),
            ),
        )
        for directory in ordered:
            document, size = self._read(directory)
            if document is None:
                continue
            documents.append(document)
            total += size
            if len(documents) > 16 or total > 65536:
                raise ToolError("项目规则超过16份或64 KiB，请精简规则后重试。")
        return RuleSelection(str(self.workspace), tuple(documents))

    def _read(self, directory: Path) -> tuple[RuleDocument | None, int]:
        """先检查授权和链接，再有界读取；不截断可能包含关键约束的规则。"""
        source = directory / "AGENTS.md"
        try:
            resolved = self.authorize(str(source))
            if resolved != source:
                raise ToolError("项目规则不能通过链接读取其他位置的文件。")
            if not resolved.exists():
                return None, 0
            if not resolved.is_file():
                raise ToolError("AGENTS.md 必须是普通文件。")
            with resolved.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ToolError("单份项目规则超过16 KiB，请精简后重试。")
            content = raw.decode("utf-8-sig")
            if "\0" in content:
                raise ToolError("项目规则必须为 UTF-8 文本。")
            scope = directory.relative_to(self.workspace).as_posix()
            return RuleDocument(
                scope,
                source.relative_to(self.workspace).as_posix(),
                sha256(raw).hexdigest(),
                content,
            ), len(raw)
        except UnicodeError as error:
            raise ToolError("项目规则必须为 UTF-8 文本。") from error
        except OSError as error:
            raise ToolError("项目规则读取失败，请检查文件和权限。") from error

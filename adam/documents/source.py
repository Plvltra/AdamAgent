"""读取配置范围内的文档；每次扫描重新检查权限、内容版本及资源上限。"""

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adam.config.settings import DocumentConfig
from adam.documents.models import DocumentError

EXCLUDED = {"node_modules", "__pycache__", "venv", "env"}


@dataclass(frozen=True)
class Source:
    """一次有界读取观察到的文件内容；version 按原始字节计算。"""

    path: str
    text: str
    version: str


class DocumentSource:
    """扫描与授权分离，authorize 由应用注入已有的路径策略。"""

    def __init__(
        self,
        workspace: Path,
        config: DocumentConfig,
        authorize: Callable[[str], Path],
        cache_dir: Path,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config = config
        self.authorize = authorize
        self.cache_dir = cache_dir.resolve()
        self.requested_roots = tuple(
            path if path.is_absolute() else self.workspace / path for path in config.roots
        )
        self.roots = tuple(path.resolve() for path in self.requested_roots)

    def scan(self) -> list[Source]:
        """返回全部合格文档；超限或文件读取失败时拒绝不完整的刷新。

        跳过隐藏项、缓存目录、链接和非 MD/TXT 文件。每个源文件重新读取并计算哈希，
        因此即使修改保持了大小和 mtime，也不会继续使用旧版本。
        """
        deadline = time.monotonic() + self.config.scan_timeout_seconds
        pending = [self.authorize(str(root)) for root in self.requested_roots]
        sources: dict[str, Source] = {}
        visited: set[Path] = set()
        entries = total_bytes = 0
        try:
            while pending:
                directory = pending.pop()
                if directory in visited or directory.is_relative_to(self.cache_dir):
                    continue
                visited.add(directory)
                directory = self.authorize(str(directory))
                if not directory.is_dir():
                    raise DocumentError("documents.roots 中的路径必须是已存在的目录。")
                with os.scandir(directory) as children:
                    for entry in children:
                        entries += 1
                        if entries > self.config.max_scan_entries or time.monotonic() > deadline:
                            raise DocumentError("文档扫描达到数量或时间上限，请缩小文档目录。")
                        path = Path(entry.path)
                        info = entry.stat(follow_symlinks=False)
                        if (
                            entry.name.startswith(".")
                            or entry.name in EXCLUDED
                            or entry.is_symlink()
                            or getattr(info, "st_file_attributes", 0) & 1024
                            or path.is_relative_to(self.cache_dir)
                        ):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in {
                            ".md",
                            ".txt",
                        }:
                            authorized = self.authorize(str(path))
                            key = str(authorized)
                            if key in sources:
                                continue
                            if len(sources) >= self.config.max_files:
                                raise DocumentError("文档数量超限，请缩小文档目录。")
                            source, count = self._read(authorized)
                            total_bytes += count
                            if total_bytes > self.config.max_total_bytes:
                                raise DocumentError("文档总字节数超限，请缩小文档目录。")
                            sources[key] = source
            if time.monotonic() > deadline:
                raise DocumentError("文档扫描超时，请缩小文档目录。")
        except (OSError, UnicodeError) as error:
            raise DocumentError(
                "文档读取失败；请检查路径、权限及 UTF-8 编码，未使用旧索引。"
            ) from error
        return sorted(sources.values(), key=lambda source: source.path)

    def _read(self, path: Path) -> tuple[Source, int]:
        """限制读取字节数，并拒绝读取期间变化的文件。"""
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(self.config.max_file_bytes + 1)
            after = os.fstat(stream.fileno())
        current = path.stat()
        if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(current):
            raise DocumentError("文档在读取时发生变化，请重新查询。")
        if len(data) > self.config.max_file_bytes:
            raise DocumentError("单个文档过大，请拆分文档或调整 documents.max_file_bytes。")
        text = data.decode("utf-8-sig")
        if "\0" in text:
            raise DocumentError("文档包含二进制内容，请使用 UTF-8 Markdown 或 TXT。")
        return Source(str(path), text, hashlib.sha256(data).hexdigest()), len(data)


def _stamp(info: os.stat_result) -> tuple[int, int, int]:
    """只比较内容相关元数据，读取自身造成的 atime 变化不算外部编辑。"""
    return info.st_size, info.st_mtime_ns, info.st_ino

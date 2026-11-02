"""为文件查询获取有界、稳定排序的目录清单；不缓存文件正文或创建跨会话状态。"""

import os
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from adam.contracts.errors import ToolError
from adam.contracts.file_query import query_digest
from adam.tool_calls.policy import PathPolicy

IGNORED = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


@dataclass(frozen=True)
class QueryEntry:
    """相对路径与扫描时元数据；文本查询还须纳入实际读取字节的摘要，不能仅靠mtime。"""

    path: str
    kind: Literal["file", "directory"]
    signature: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class QueryInventory:
    """清单只含授权的普通文件/目录；忽略与拒绝的条目只计数，不泄露实际路径。"""

    root: Path
    recursive: bool
    entries: tuple[QueryEntry, ...]
    skipped_entries: int

    @property
    def generation(self) -> str:
        """目录元数据摘要，仅用于观察间比较，不保证跨文件原子快照。"""
        return query_digest(
            {
                "root": str(self.root),
                "recursive": self.recursive,
                "entries": [asdict(entry) for entry in self.entries],
                "skipped_entries": self.skipped_entries,
            }
        )


def capture_inventory(
    root: Path,
    policy: PathPolicy,
    *,
    recursive: bool,
    max_entries: int,
    check: Callable[[], None],
) -> QueryInventory:
    """按实际授权扫描并排序；每项调用check响应取消/期限，扫描不完整就失败。

    max_entries 限制每次扫描触及的目录项（包括跳过项）；root本身单独保留。
    symlink/junction 不递归，避免循环和边界逃逸。再次调用可检测目录变化，
    仍不替代文件字节版本或文件锁；异常退出也会关闭 scandir。
    """
    if type(max_entries) is not int or not 1 <= max_entries <= 1_000_000:
        raise ToolError("目录扫描上限必须是1～1000000的整数。")
    check()
    root = policy.resolve(str(root))
    entries = [_entry(root, ".")]
    if entries[0].kind == "file":
        return QueryInventory(root, recursive, tuple(entries), 0)
    pending, scanned, skipped = [root], 0, 0
    while pending:
        check()
        directory = pending.pop()
        # 授权后再次核对实际路径；目录被替换成越界链接时不继续遍历。
        if policy.resolve(str(directory)) != directory:
            raise ToolError("查询目录在扫描期间发生变化，请重试。")
        with os.scandir(directory) as iterator:
            for item in iterator:
                check()
                scanned += 1
                if scanned > max_entries:
                    raise ToolError("扫描达到数量上限，请缩小搜索目录。")
                if item.is_symlink() or (recursive and item.name in IGNORED):
                    skipped += 1
                    continue
                try:
                    path = policy.resolve(item.path)
                except ToolError:
                    skipped += 1
                    continue
                if path != Path(item.path).absolute():
                    skipped += 1
                    continue
                entry = _entry(path, path.relative_to(root).as_posix())
                entries.append(entry)
                if recursive and entry.kind == "directory":
                    pending.append(path)
    check()
    return QueryInventory(root, recursive, tuple(sorted(entries, key=lambda e: e.path)), skipped)


def _entry(path: Path, relative: str) -> QueryEntry:
    """读取单个对象的类型与元数据，非普通文件/目录不能加入可搜索清单。"""
    details = path.stat()
    if stat.S_ISDIR(details.st_mode):
        kind = "directory"
    elif stat.S_ISREG(details.st_mode):
        kind = "file"
    else:
        raise ToolError("扫描遇到非普通文件或目录，无法保证查询完整。")
    return QueryEntry(
        relative,
        kind,
        (
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
            details.st_ino,
            details.st_dev,
            details.st_mode,
        ),
    )

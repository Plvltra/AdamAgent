"""有界、可刷新的内存代码索引；每次重读当前授权字节，只缓存未变化文件的解析结果。"""

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import RLock

from adam.code_index.models import CodeIndexError, CodeIssue, CodeLimits, CodeModule, CodeSnapshot
from adam.code_index.parser import module_name, parse_module
from adam.contracts.errors import ToolError

IGNORED = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


@dataclass
class _Scan:
    """同次扫描共用的计数；目录项、原字节和符号分别限额。"""

    entries: int = 0
    files: int = 0
    bytes: int = 0
    symbols: int = 0
    skipped: int = 0


class CodeIndex:
    """当前源码的结构快照；刷新串行，失败不返回旧快照，不向目标项目写缓存。"""

    def __init__(
        self,
        root: Path,
        authorize: Callable[[str], Path],
        limits: CodeLimits | None = None,
        check_cancel: Callable[[], None] | None = None,
    ) -> None:
        """root 指向一个 .py 文件或目录；authorize 必须逐次执行宿主当前路径权限检查。"""
        self.root = root.expanduser().absolute()
        self.authorize = authorize
        self.limits = limits or CodeLimits()
        self.check_cancel = check_cancel or (lambda: None)
        self._cache: dict[Path, CodeModule] = {}
        self._lock = RLock()

    def refresh(self) -> CodeSnapshot:
        """重读并原子替换内存索引，处理同时间戳更新、删除和权限撤销。

        每个文件的语法、编码或读取问题保存在该文件诊断中，旧定义不再参与查询。
        整体数量/字节/超时超限会失败，取消原样传播；两者都不返回部分或旧快照。
        """
        self.check_cancel()
        with self._lock:
            deadline = time.monotonic() + self.limits.timeout_seconds

            def check() -> None:
                self.check_cancel()
                if time.monotonic() > deadline:
                    raise CodeIndexError("代码扫描或解析超过时间上限，请缩小目录。")

            check()
            root = self.authorize(str(self.root))
            base = root if root.is_dir() else root.parent
            scan = _Scan()
            modules: dict[Path, CodeModule] = {}
            try:
                for path in self._walk(root, scan, check):
                    check()
                    scan.files += 1
                    if scan.files > self.limits.max_files:
                        raise CodeIndexError("Python 文件数量超限，请缩小索引目录。")
                    relative = path.relative_to(base)
                    modules[path] = self._module(path, relative, scan, check)
                    scan.symbols += len(modules[path].definitions) + len(modules[path].imports)
                    if scan.symbols > self.limits.max_symbols:
                        raise CodeIndexError("代码定义和导入总数超限，请缩小目录。")
                check()
            except OSError as error:
                raise CodeIndexError("代码目录扫描失败，未返回旧索引。") from error
            ordered = tuple(modules[path] for path in sorted(modules))
            identity = [
                str(root),
                [
                    [item.path, item.version, item.issue.reason if item.issue else None]
                    for item in ordered
                ],
                scan.skipped,
            ]
            generation = sha256(
                json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            # 只有整个范围扫描成功才替换缓存；下一次仍重新授权、读字节并核对版本。
            self._cache = modules
            return CodeSnapshot(str(root), ordered, generation, scan.skipped)

    def _walk(self, root: Path, scan: _Scan, check: Callable[[], None]) -> Iterator[Path]:
        """不递归链接或缓存目录；遍历中每个可进入路径都通过宿主授权。"""
        if root.is_file():
            if root.suffix.lower() != ".py":
                raise CodeIndexError("代码索引只接受 .py 文件或包含 Python 文件的目录。")
            yield root
            return
        if not root.is_dir() or root.name in IGNORED:
            raise CodeIndexError("代码索引根不是可扫描目录。")
        stack = [root]
        while stack:
            check()
            directory = stack.pop()
            # 重新检查父目录，防止上次扫描后权限撤销或路径改为链接。
            if self.authorize(str(directory)) != directory:
                raise CodeIndexError("目录真实路径已改变，请重新选择索引范围。")
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    check()
                    scan.entries += 1
                    if scan.entries > self.limits.max_scan_entries:
                        raise CodeIndexError("目录项数量超限，请缩小索引范围。")
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                check()
                if entry.name in IGNORED or entry.is_symlink():
                    scan.skipped += 1
                    continue
                try:
                    path = self.authorize(entry.path)
                except ToolError:
                    scan.skipped += 1
                    continue
                # PathPolicy 会解析 Windows junction；禁止用扫描递归穿过它。
                if path != Path(entry.path).absolute():
                    scan.skipped += 1
                    continue
                if path.is_dir():
                    stack.append(path)
                elif path.is_file() and path.suffix.lower() == ".py":
                    yield path

    def _module(
        self, path: Path, relative: Path, scan: _Scan, check: Callable[[], None]
    ) -> CodeModule:
        """同一有界读取既用于版本比较，也用于解析；读取失败不复用旧符号。"""
        if self.authorize(str(path)) != path:
            raise CodeIndexError("源文件真实路径已改变，请重新查询。")
        try:
            parts = []
            remaining = self.limits.max_file_bytes + 1
            with path.open("rb") as stream:
                while remaining:
                    check()
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
                    scan.bytes += len(chunk)
                    if scan.bytes > self.limits.max_total_bytes:
                        raise CodeIndexError("代码读取总字节数超限，请缩小目录。")
            raw = b"".join(parts)
        except OSError:
            return CodeModule(
                str(path),
                relative.as_posix(),
                module_name(relative),
                None,
                issue=CodeIssue("read_error", "当前文件无法完整读取，未使用旧定义。"),
            )
        check()
        previous = self._cache.get(path)
        if (
            previous is not None
            and previous.version == sha256(raw).hexdigest()
            and previous.relative_path == relative.as_posix()
        ):
            return previous
        return parse_module(path, relative, raw, self.limits, check)

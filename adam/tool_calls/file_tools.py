"""文件查询工具；接收已授权路径，返回尚未序列化的工具结果。"""

import fnmatch
import os
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

from adam.tool_calls.policy import PathPolicy, ToolError, ToolLimits
from adam.tool_calls.text_file import read_snapshot

IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}


class FileTools:
    def __init__(self, policy: PathPolicy, limits: ToolLimits) -> None:
        self.policy = policy
        self.limits = limits

    def file_info(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回文件类型、大小与修改时间。"""
        stat = path.stat()
        return {
            "path": str(path),
            "kind": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }

    def list_directory(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """按 offset 返回排序后的目录项及下一页位置。"""
        offset = args.get("offset", 0)
        if offset < 0 or not path.is_dir():
            raise ToolError("offset必须非负，path必须是目录。")
        entries = []
        with os.scandir(path) as scan:
            for entry in scan:
                if len(entries) >= self.limits.max_scan_entries:
                    raise ToolError("目录过大，请使用更小的目录。")
                entries.append(entry.name)
        entries.sort()
        return {
            "entries": entries[offset : offset + 200],
            "next_offset": offset + 200 if len(entries) > offset + 200 else None,
        }

    def read_file(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """按行展示同一读取快照；返回完整文件版本、行号、截断与续读位置。"""
        start, count = args.get("start_line", 1), args.get("max_lines", self.limits.max_read_lines)
        if start < 1 or not 1 <= count <= self.limits.max_read_lines:
            raise ToolError("读取行号或行数超限。")
        snapshot = read_snapshot(path, self.limits.max_text_file_bytes)
        lines, chars, next_line, truncated = [], 0, None, False
        for number, line in enumerate(StringIO(snapshot.text, newline=None), 1):
            line = line.rstrip("\r\n")
            if number < start:
                continue
            if len(lines) >= count or chars >= self.limits.max_result_chars // 2:
                next_line = number
                truncated = True
                break
            text = line[: min(1000, self.limits.max_result_chars // 4)]
            truncated |= len(text) < len(line)
            lines.append({"line": number, "text": text})
            chars += len(text) + 40
        return {
            "version": snapshot.version,
            "lines": lines,
            "truncated": truncated,
            "next_line": next_line,
        }

    def find_files(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回匹配 glob 的相对文件路径，最多 200 项。"""
        results = []
        for _file, relative in self._matching_files(path, args["pattern"]):
            results.append(relative)
            if len(results) >= 200:
                return {"results": results, "truncated": True}
        return {"results": results, "truncated": False}

    def search_text(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """搜索字面文本；跳过不可读文件，返回最多 50 个带行号的匹配项。"""
        query = args["query"]
        if not query:
            raise ToolError("搜索文本不能为空。")
        results = []
        for file, relative in self._matching_files(path, args.get("pattern", "*")):
            try:
                for number, line in self._lines(file):
                    if query in line:
                        results.append({"path": relative, "line": number, "text": line[:500]})
                        if len(results) >= 50:
                            return {"results": results, "truncated": True}
            except (UnicodeError, ToolError, OSError):
                continue
        return {"results": results, "truncated": False}

    def _matching_files(self, path: Path, pattern: str) -> Iterator[tuple[Path, str]]:
        base = path if path.is_dir() else path.parent
        for file in self._walk(path):
            relative = file.relative_to(base).as_posix()
            if fnmatch.fnmatch(relative, pattern) or file.match(pattern):
                yield file, relative

    def _walk(self, root: Path) -> Iterator[Path]:
        if root.is_file():
            yield root
            return
        deadline = time.monotonic() + self.limits.scan_timeout_seconds
        stack = [root]
        scanned = 0
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > self.limits.max_scan_entries or time.monotonic() > deadline:
                        raise ToolError("扫描达到数量或时间上限，请缩小搜索目录。")
                    if entry.name in IGNORED or entry.is_symlink():
                        continue
                    try:
                        path = self.policy.resolve(entry.path)
                    except ToolError:
                        continue
                    # Windows junction也不递归，避免循环和边界逃逸。
                    if path != Path(entry.path).absolute():
                        continue
                    if path.is_dir():
                        stack.append(path)
                    elif path.is_file():
                        yield path

    def _lines(self, path: Path) -> Iterator[tuple[int, str]]:
        if not path.is_file() or path.stat().st_size > self.limits.max_text_file_bytes:
            raise ToolError("不是普通文件或超过文件扫描上限。")
        with path.open("r", encoding="utf-8-sig") as file:
            for number, line in enumerate(file, 1):
                if "\0" in line:
                    raise ToolError("不支持二进制文件。")
                yield number, line.rstrip("\r\n")

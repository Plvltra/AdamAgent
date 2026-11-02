"""文件查询工具；接收已授权路径，返回尚未序列化的工具结果。"""

from io import StringIO
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.tool_calls.file_queries import FileQueries
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot


class FileTools:
    def __init__(
        self, policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
    ) -> None:
        """每次读取可绑定独立取消令牌，路径规则与限制只读共享。"""
        self.policy = policy
        self.limits = limits
        self._cancel = cancel
        self._queries = FileQueries(policy, limits, cancel)

    def file_info(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回文件类型、大小与修改时间。"""
        self._check_cancel()
        stat = path.stat()
        return {
            "path": str(path),
            "kind": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }

    def list_directory(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """稳定列出授权直接子项；优先使用绑定版本的cursor，旧offset仅保留兼容。"""
        return self._queries.run("list_directory", path, args)

    def read_file(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """按行展示同一读取快照，保留实际路径、完整文件版本、行号、截断与续读位置。

        path 由调用适配器解析；来源随结果保存，后续提案不重新猜测原路径指向。
        """
        start, count = args.get("start_line", 1), args.get("max_lines", self.limits.max_read_lines)
        if start < 1 or not 1 <= count <= self.limits.max_read_lines:
            raise ToolError("读取行号或行数超限。")
        snapshot = read_snapshot(path, self.limits.max_text_file_bytes, self._cancel)
        lines, chars, next_line, truncated = [], 0, None, False
        for number, line in enumerate(StringIO(snapshot.text, newline=None), 1):
            self._check_cancel()
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
            "path": str(path),
            "version": snapshot.version,
            "lines": lines,
            "truncated": truncated,
            "next_line": next_line,
        }

    def find_files(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """按glob稳定查询授权文件；返回完整页面和来源游标，可继续读取后续结果。"""
        return self._queries.run("find_files", path, args)

    def search_text(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """默认字面、显式正则查询；返回版本与行号，跳过文件计数，超限/取消不返回部分页。"""
        return self._queries.run("search_text", path, args)

    def _check_cancel(self) -> None:
        """在扫描与文本处理边界响应取消，不把取消当作跳过文件的普通错误。"""
        if self._cancel:
            self._cancel.check()

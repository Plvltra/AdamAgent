"""稳定的文件/文本查询与无状态续读；每页重查权限和来源，不缓存完整结果集。"""

import fnmatch
import json
import time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.contracts.file_query import QueryCursor, decode_cursor, query_digest
from adam.tool_calls.file_evidence import FileStateError
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.query_scan import QueryInventory, capture_inventory
from adam.tool_calls.text_file import TextSnapshot, read_snapshot


class FileQueries:
    """一次查询只保留有界清单、版本摘要与单页结果；复用原读取权限和取消令牌。"""

    def __init__(
        self, policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
    ) -> None:
        self.policy, self.limits, self.cancel = policy, limits, cancel

    def run(self, kind: str, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回完整条目和next_cursor；来源变化、期限或总读取上限失败，不返回半页。

        每页重新扫描，因此游标不授予路径权限。目录元数据复查能检测通常的并发修改，
        文本版本还绑定实际读取字节；这不是跨文件锁或文件系统原子快照。
        """
        limit, offset = _paging(kind, args)
        pattern, query = args.get("pattern", "*"), args.get("query", "")
        if not pattern or len(pattern) > 1024:
            raise ToolError("文件glob必须为1～1024个字符。")
        if kind == "search_text" and (not query or len(query) > 4096):
            raise ToolError("搜索文本必须为1～4096个字符。")
        query_key = query_digest(
            {"kind": kind, "path": str(path), "pattern": pattern, "query": query, "mode": "literal"}
        )
        try:
            cursor = decode_cursor(args["cursor"], query_key) if "cursor" in args else None
        except ValueError as error:
            raise ToolError(str(error)) from error
        if cursor:
            offset = cursor.offset
        deadline = time.monotonic() + self.limits.scan_timeout_seconds

        def check() -> None:
            if self.cancel:
                self.cancel.check()
            if time.monotonic() > deadline:
                raise ToolError("扫描达到时间上限，请缩小搜索目录。")

        def inventory() -> QueryInventory:
            return capture_inventory(
                path,
                self.policy,
                recursive=kind != "list_directory",
                max_entries=self.limits.max_scan_entries,
                check=check,
            )

        initial = inventory()
        if kind == "list_directory" and initial.entries[0].kind != "directory":
            raise ToolError("list_directory的path必须是目录。")
        page = _Page(kind, query_key, offset, limit, self.limits.max_result_chars)
        sources, skipped = self._collect(initial, kind, pattern, query, page, check)
        if inventory().generation != initial.generation:
            raise ToolError("查询来源在扫描期间发生变化，请从第一页重试。")
        generation = query_digest({"inventory": initial.generation, "sources": sources})
        if cursor and cursor.generation != generation:
            raise ToolError("查询来源已变化，旧游标失效，请从第一页重新查询。")
        check()
        return page.finish(generation, initial.skipped_entries, skipped)

    def _collect(
        self,
        initial: QueryInventory,
        kind: str,
        pattern: str,
        query: str,
        page: "_Page",
        check: Callable[[], None],
    ) -> tuple[list[dict[str, Any]], int]:
        """依清单顺序计数并收集一页；文本逐文件读取，达到页尾后仍核对完整查询来源。"""
        sources: list[dict[str, Any]] = []
        remaining, skipped = self.limits.max_scan_bytes, 0
        path = initial.root
        for entry in initial.entries:
            check()
            if kind == "list_directory":
                if entry.path != ".":
                    page.offer(entry.path)
                continue
            if entry.kind != "file":
                continue
            file = path if entry.path == "." else path / entry.path
            relative = path.name if entry.path == "." else entry.path
            if not (fnmatch.fnmatch(relative, pattern) or file.match(pattern)):
                continue
            if kind == "find_files":
                page.offer(relative)
                continue
            snapshot, used, source = self._read(file, entry.signature[0], remaining)
            check()
            remaining -= used
            sources.append({"path": relative, **source})
            if snapshot is None:
                skipped += 1
                continue
            for number, line in enumerate(StringIO(snapshot.text, newline=None), 1):
                check()
                if query in line:
                    text = line.rstrip("\r\n")
                    page.offer(
                        {
                            "path": relative,
                            "line": number,
                            "text": text[:500],
                            "version": source["version"],
                            "line_truncated": len(text) > 500,
                        }
                    )
        return sources, skipped

    def _read(
        self, file: Path, observed_size: int, remaining: int
    ) -> tuple[TextSnapshot | None, int, dict[str, Any]]:
        """读取前后重新授权；不可读项保留跳过事实，总字节上限不能变成跳过后成功。

        成功按实际字节计费；无法取得完整字节数的失败按本次允许读取上限保守扣减。
        每次最多多读一个字节检测增长，因此总预算预留这个字节。
        """
        if self.policy.resolve(str(file)) != file:
            raise ToolError("查询文件的实际路径已变化。")
        if observed_size > self.limits.max_text_file_bytes:
            return None, 0, {"status": "too_large"}
        if remaining <= observed_size:
            raise ToolError("搜索达到总读取字节上限，请缩小搜索目录。")
        allowed = min(self.limits.max_text_file_bytes, remaining - 1)
        try:
            snapshot = read_snapshot(file, allowed, self.cancel)
        except FileStateError as error:
            result = (None, allowed + 1, {"status": "not_text", "version": error.version})
        except (ToolError, OSError):
            result = (None, allowed + 1, {"status": "unreadable"})
        else:
            result = (snapshot, len(snapshot.raw), {"status": "read", "version": snapshot.version})
        if self.policy.resolve(str(file)) != file:
            raise ToolError("查询文件的实际路径已变化。")
        return result


class _Page:
    """流式计算条目位置，只保留一个可被原编码器完整输出的页面。"""

    def __init__(self, kind: str, query: str, offset: int, limit: int, maximum: int) -> None:
        self.kind, self.query, self.offset, self.limit, self.maximum = (
            kind,
            query,
            offset,
            limit,
            maximum,
        )
        self.items: list[Any] = []
        self.total = 0
        self.full = False

    def offer(self, item: str | dict[str, Any]) -> None:
        self.total += 1
        if self.total <= self.offset or self.full:
            return
        candidate = self._result("f" * 64, 1000000, 1000000, self.items + [item], True)
        if len(json.dumps({"ok": True, **candidate}, ensure_ascii=False)) > self.maximum:
            if not self.items:
                raise ToolError(
                    "单个搜索条目超过结果长度上限，请提高max_result_chars或缩小查询范围。"
                )
            self.full = True
            return
        self.items.append(item)
        self.full = len(self.items) >= self.limit

    def finish(self, generation: str, skipped_entries: int, skipped_files: int) -> dict[str, Any]:
        if self.offset > self.total:
            raise ToolError("游标位置超过结果范围，请从第一页重新查询。")
        more = self.offset + len(self.items) < self.total
        return self._result(generation, skipped_entries, skipped_files, self.items, more)

    def _result(
        self,
        generation: str,
        skipped_entries: int,
        skipped_files: int,
        items: list[Any],
        more: bool,
    ) -> dict[str, Any]:
        position = self.offset + len(items)
        result = {
            "entries" if self.kind == "list_directory" else "results": items,
            "generation": generation,
            "next_cursor": QueryCursor(self.query, generation, position).encode() if more else None,
            "truncated": more,
            "skipped_entries": skipped_entries,
            "skipped_files": skipped_files,
        }
        if self.kind == "list_directory":
            # 兼容旧调用的offset；它不绑定版本，新调用应使用next_cursor。
            result["next_offset"] = position if more else None
        return result


def _paging(kind: str, args: dict[str, Any]) -> tuple[int, int]:
    """页大小只控制返回条目，不控制授权或总扫描量；旧offset仅用于直接目录。"""
    maximum = 50 if kind == "search_text" else 200
    limit, offset = args.get("limit", maximum), args.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ToolError(f"limit必须为1～{maximum}的整数。")
    if type(offset) is not int or not 0 <= offset <= 1000000000:
        raise ToolError("offset必须为0～1000000000的整数。")
    if "cursor" in args and "offset" in args:
        raise ToolError("cursor与旧offset不能同时使用。")
    return limit, offset

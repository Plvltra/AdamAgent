"""授权会话的只读内存索引；读取完整原日志，不切换会话、不恢复或改写 JSONL。"""

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from adam.contracts.models import valid_session_id
from adam.contracts.session_search import (
    SessionExcerpt,
    SessionSearchIssue,
    SessionSearchLimits,
    SessionSearchResult,
)
from adam.session.log import parse_log


@dataclass(frozen=True)
class _Message:
    session_id: str
    turn_id: str
    message_index: int | None
    line: int
    kind: str
    role: str
    status: str
    reason: str | None
    title: str
    digest: str
    snapshot_digest: str
    text: str


class _SourceError(Exception):
    """固定错误码不包含源正文或底层异常中的路径。"""

    def __init__(self, code: str, bytes_read: int = 0) -> None:
        super().__init__(code)
        self.bytes_read = bytes_read


class SessionSearchIndex:
    """只索引宿主提供的会话 ID，最多 32 个；目录发现不自动授予读取权限。

    缓存只节省解析，每次查询仍重读并哈希授权日志，更新、删除或损坏后不返回旧片段。
    实例不跨线程共享；原文与索引只在内存，禁止通过本接口触发会话恢复。
    """

    def __init__(
        self,
        root: Path,
        allowed_session_ids: tuple[str, ...],
        limits: SessionSearchLimits | None = None,
    ) -> None:
        if (
            not isinstance(allowed_session_ids, tuple)
            or len(allowed_session_ids) > 32
            or any(
                not isinstance(sid, str) or not valid_session_id(sid) for sid in allowed_session_ids
            )
            or len(set(allowed_session_ids)) != len(allowed_session_ids)
        ):
            raise ValueError("invalid authorized session IDs")
        self.root = root.resolve()
        self.allowed_ids = allowed_session_ids
        self.limits = limits or SessionSearchLimits()
        self._cache: dict[str, tuple[str, tuple[_Message, ...]]] = {}

    def search(
        self, query: str, *, limit: int = 8, include_diagnostics: bool = False
    ) -> SessionSearchResult:
        """按关键词返回成功消息原文；显式开启后才加入带状态标签的失败/未完成输入。

        某来源损坏会报告 issue，仍可查其余来源；全局扫描或总预算失败时不返回部分命中。
        查询不检索系统提示、模型推理字段、未提交的回答或摘要改写文本。
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("query must contain 1 to 1000 characters")
        if type(limit) is not int or not 1 <= limit <= 20 or type(include_diagnostics) is not bool:
            raise ValueError("invalid search options")
        messages, issues = self._refresh()
        terms = _terms(query)
        # 索引键指向本次有效的原始消息；来源失败后整份缓存被替换，不残留旧倒排项。
        inverted: dict[str, set[int]] = {}
        for position, message in enumerate(messages):
            if message.kind != "message" and not include_diagnostics:
                continue
            for term in _terms(message.text):
                inverted.setdefault(term, set()).add(position)
        scores: dict[int, int] = {}
        for term in terms:
            for position in inverted.get(term, ()):
                scores[position] = scores.get(position, 0) + 1
        ranked = sorted(
            scores, key=lambda p: (-scores[p], messages[p].session_id, messages[p].line)
        )
        hits = []
        for position in ranked[:limit]:
            message = messages[position]
            anchor = re.search(re.escape(query), message.text, re.IGNORECASE)
            if anchor is None:
                anchor = next(
                    (
                        match
                        for term in sorted(terms)
                        if (match := re.search(re.escape(term), message.text, re.IGNORECASE))
                    ),
                    None,
                )
            start = max(0, (anchor.start() if anchor else 0) - 100)
            hits.append(
                self._excerpt(message, start, self.limits.max_excerpt_chars, scores[position])
            )
        return SessionSearchResult(tuple(hits), tuple(issues), len(ranked))

    def read(
        self,
        session_id: str,
        turn_id: str,
        message_index: int | None,
        source_sha256: str,
        *,
        kind: str = "message",
        start: int = 0,
        chars: int = 1000,
    ) -> SessionExcerpt:
        """按检索引用读取同一版本的原文；原事件、位置或状态变化必须重新搜索。

        输入 ID 仅用于缩小已授权范围，不会扩权；诊断引用须显式使用 diagnostic_input。
        只追加其他事件不使原文引用失效，返回值同时记录本次完整文件的快照哈希。
        """
        if session_id not in self.allowed_ids:
            raise ValueError("session is not authorized")
        if (
            kind not in {"message", "diagnostic_input"}
            or not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
            or type(start) is not int
            or start < 0
            or type(chars) is not int
            or not 1 <= chars <= self.limits.max_excerpt_chars
            or (kind == "message" and (type(message_index) is not int or message_index < 1))
            or (kind == "diagnostic_input" and message_index is not None)
        ):
            raise ValueError("invalid source reference")
        messages, issues = self._refresh()
        if any(issue.session_id in {None, session_id} for issue in issues):
            raise ValueError("source unavailable; search again after resolving the issue")
        found = next(
            (
                m
                for m in messages
                if (m.session_id, m.turn_id, m.message_index, m.kind)
                == (session_id, turn_id, message_index, kind)
            ),
            None,
        )
        if found is None or found.digest != source_sha256:
            raise ValueError("source changed or reference is missing; search again")
        if start >= len(found.text):
            raise ValueError("excerpt start is outside the source")
        return self._excerpt(found, start, chars, 0)

    def _refresh(self) -> tuple[list[_Message], list[SessionSearchIssue]]:
        """按文件字节验证缓存，来源无效立即淘汰；不在失败时借用上一轮结果。"""
        if not self.allowed_ids:
            return [], []
        fresh: dict[str, tuple[str, tuple[_Message, ...]]] = {}
        issues: list[SessionSearchIssue] = []
        messages: list[_Message] = []
        try:
            catalog = self._catalog()
            total = 0
            for sid in self.allowed_ids:
                paths = catalog.get(sid, [])
                if len(paths) != 1:
                    issues.append(
                        SessionSearchIssue(sid, "missing" if not paths else "duplicate_id")
                    )
                    continue
                try:
                    raw = self._read_bytes(paths[0], self.limits.max_total_bytes - total)
                    total += len(raw)
                    if total > self.limits.max_total_bytes:
                        raise _SourceError("total_byte_limit")
                    digest = hashlib.sha256(raw).hexdigest()
                    cached = self._cache.get(sid)
                    entries = (
                        cached[1]
                        if cached and cached[0] == digest
                        else self._decode(raw, sid, digest)
                    )
                except _SourceError as error:
                    total += error.bytes_read
                    if str(error) == "total_byte_limit" or total > self.limits.max_total_bytes:
                        raise _SourceError("total_byte_limit") from error
                    issues.append(SessionSearchIssue(sid, str(error)))
                    continue
                if len(messages) + len(entries) > self.limits.max_messages:
                    raise _SourceError("message_limit")
                fresh[sid] = (digest, entries)
                messages.extend(entries)
        except _SourceError as error:
            self._cache.clear()
            return [], [SessionSearchIssue(None, str(error))]
        self._cache = fresh
        return messages, issues

    def _catalog(self) -> dict[str, list[Path]]:
        """只走 YYYY/MM/DD 层级，扫描条目有总上限；不枚举子 Agent、备份或其他目录。"""
        found: dict[str, list[Path]] = {}
        pending: list[tuple[Path, tuple[int, ...]]] = [(self.root, ())]
        scanned = 0
        try:
            while pending:
                directory, parts = pending.pop()
                self._inside(directory)
                with os.scandir(directory) as stream:
                    for entry in stream:
                        scanned += 1
                        if scanned > self.limits.max_scan_entries:
                            raise _SourceError("scan_limit")
                        path = Path(entry.path)
                        if len(parts) == 3:
                            if path.suffix == ".jsonl" and path.stem in self.allowed_ids:
                                found.setdefault(path.stem, []).append(path)
                            continue
                        if re.fullmatch(r"\d{4}" if not parts else r"\d{2}", entry.name) is None:
                            continue
                        values = (*parts, int(entry.name))
                        try:
                            date(
                                values[0],
                                values[1] if len(values) > 1 else 1,
                                values[2] if len(values) > 2 else 1,
                            )
                        except ValueError:
                            continue
                        if entry.is_dir():
                            pending.append((path, values))
        except OSError as error:
            raise _SourceError("scan_unavailable") from error
        return found

    def _inside(self, path: Path) -> Path:
        """解析链接后仍须位于固定根目录；不把目录外的同名文件当成授权会话。"""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as error:
            raise _SourceError("path_unavailable") from error
        if not resolved.is_relative_to(self.root):
            raise _SourceError("outside_root")
        return resolved

    def _read_bytes(self, path: Path, remaining: int) -> bytes:
        """读同一个普通文件句柄的有界快照，检测替换或追加竞争，不执行写锁恢复。"""
        raw = b""
        try:
            resolved = self._inside(path)
            with resolved.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise _SourceError("not_regular_file")
                if before.st_size > self.limits.max_file_bytes:
                    raise _SourceError("file_byte_limit")
                if before.st_size > remaining:
                    raise _SourceError("total_byte_limit")
                raw = stream.read(min(remaining, self.limits.max_file_bytes) + 1)
                after = os.fstat(stream.fileno())
            current = path.stat()
            if len(raw) > remaining:
                raise _SourceError("total_byte_limit")
            if len(raw) > self.limits.max_file_bytes:
                raise _SourceError("file_byte_limit")
            if (
                self._inside(path) != resolved
                or any(
                    (s.st_size, s.st_mtime_ns, s.st_ino)
                    != (before.st_size, before.st_mtime_ns, before.st_ino)
                    for s in (after, current)
                )
                or len(raw) != before.st_size
            ):
                raise _SourceError("source_changing")
            return raw
        except _SourceError as error:
            error.bytes_read = len(raw)
            raise
        except OSError as error:
            raise _SourceError("read_unavailable", len(raw)) from error

    @staticmethod
    def _decode(raw: bytes, sid: str, digest: str) -> tuple[_Message, ...]:
        """只投影已提交消息；失败/未完成输入单独标记，系统提示和推理字段不进入索引。"""
        try:
            log = parse_log(raw, sid)
            entries = []
            counters: dict[str, int] = {}
            title = log.metadata["title"][:200]
            for line, event in enumerate(log.events, 1):
                payload = event["payload"]
                turn_id = payload.get("turn_id")
                if turn_id not in log.turns:
                    continue
                state = log.turns[turn_id]
                anchor = hashlib.sha256(
                    json.dumps(
                        [sid, line, event, state["status"], state.get("reason")],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if event["type"] == "message" and state["status"] == "succeeded":
                    counters[turn_id] = counters.get(turn_id, 0) + 1
                    message = payload["message"]
                    if isinstance(message.get("content"), str) and message["content"]:
                        entries.append(
                            _Message(
                                sid,
                                turn_id,
                                counters[turn_id],
                                line,
                                "message",
                                message["role"],
                                "succeeded",
                                state.get("reason"),
                                title,
                                anchor,
                                digest,
                                message["content"],
                            )
                        )
                elif event["type"] == "turn_started" and state["status"] != "succeeded":
                    entries.append(
                        _Message(
                            sid,
                            turn_id,
                            None,
                            line,
                            "diagnostic_input",
                            "user",
                            state["status"],
                            state.get("reason"),
                            title,
                            anchor,
                            digest,
                            payload["user_text"],
                        )
                    )
            return tuple(entries)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            raise _SourceError("invalid_log") from error

    @staticmethod
    def _excerpt(message: _Message, start: int, chars: int, score: int) -> SessionExcerpt:
        end = min(len(message.text), start + chars)
        return SessionExcerpt(
            message.session_id,
            message.turn_id,
            message.message_index,
            message.line,
            message.kind,
            message.role,
            message.status,
            message.reason,
            message.title,
            message.digest,
            message.snapshot_digest,
            start,
            end,
            len(message.text),
            message.text[start:end],
            score,
        )


def _terms(text: str) -> set[str]:
    """用英文词和中文相邻双字构建本地倒排索引，不作语义或事实正确性判断。"""
    result = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(segment[i : i + 2] for i in range(len(segment) - 1))
    return result

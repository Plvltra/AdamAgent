"""每个会话一个 JSONL 文件；只加载完整的成功问答。"""

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from filelock import FileLock, Timeout


@dataclass(frozen=True)
class Session:
    session_id: str
    title: str
    system_prompt: str


@dataclass(frozen=True)
class Message:
    message_id: str
    session_id: str
    position: int
    role: str
    content: str


class StorageError(OSError):
    """会话占用、格式损坏或写入失败。"""


class SessionNotFoundError(LookupError):
    """指定会话不存在。"""


def valid_session_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{8}", value) is not None


def record(kind: str, payload: dict[str, object]) -> bytes:
    value = {"type": kind, "timestamp": datetime.now(UTC).isoformat(), "payload": payload}
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class SessionStore:
    """当前选中会话持有写锁；切换成功或关闭时释放，不跨线程共享实例。"""

    def __init__(self, path: Path) -> None:
        self.root = path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".locks").mkdir(exist_ok=True)
        self._lock: FileLock | None = None
        self._active_id: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self._active_id = None

    def _path(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise SessionNotFoundError("会话 ID 必须是 8 位小写十六进制字符。")
        paths = list(self.root.glob(f"*/*/*/{session_id}.jsonl"))
        if not paths:
            raise SessionNotFoundError(f"会话 {session_id} 不存在。")
        if len(paths) != 1:
            raise StorageError(f"会话 {session_id} 有重复文件，请检查日期目录。")
        return paths[0]

    def create_session(self, title: str, system_prompt: str) -> str:
        # 根目录锁保证跨日期、跨进程生成的短 ID 不重复。
        try:
            with FileLock(self.root / ".locks" / "create.lock", timeout=0):
                for _ in range(100):
                    session_id = secrets.token_hex(4)
                    if not list(self.root.glob(f"*/*/*/{session_id}.jsonl")):
                        break
                else:
                    raise StorageError("无法分配新的会话 ID。")
                directory = self.root / datetime.now().strftime("%Y/%m/%d")
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{session_id}.jsonl"
                payload = {
                    "version": 1,
                    "id": session_id,
                    "title": title,
                    "system_prompt": system_prompt,
                }
                with path.open("xb") as file:
                    file.write(record("session_meta", payload))
                    file.flush()
                    os.fsync(file.fileno())
        except Timeout:
            raise StorageError("其他进程正在创建会话，请稍后重试。") from None
        return session_id

    def _read(self, session_id: str) -> tuple[Session, list[Message]]:
        path = self._path(session_id)
        try:
            raw = path.read_bytes()
            if not raw.endswith(b"\n"):
                raise ValueError("missing newline")
            entries = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("timestamp"), str):
                    raise ValueError("invalid record")
                if not isinstance(entry.get("payload"), dict):
                    raise ValueError("invalid payload")
            meta = entries[0]
            data = meta["payload"]
            if (
                meta.get("type") != "session_meta"
                or type(data.get("version")) is not int
                or data["version"] != 1
                or data.get("id") != session_id
            ):
                raise ValueError("invalid metadata")
            if not all(isinstance(data.get(key), str) for key in ("title", "system_prompt")):
                raise ValueError("invalid metadata fields")
            session = Session(session_id, data["title"], data["system_prompt"])
            messages = []
            seen_turns: set[str] = set()
            turn_id = ""
            for position, entry in enumerate(entries[1:]):
                payload = entry["payload"]
                role = "user" if position % 2 == 0 else "assistant"
                if (
                    entry.get("type") != "message"
                    or payload.get("role") != role
                    or not isinstance(payload.get("content"), str)
                    or not isinstance(payload.get("turn_id"), str)
                    or not payload["turn_id"]
                ):
                    raise ValueError("invalid message")
                if role == "user":
                    turn_id = payload["turn_id"]
                    if turn_id in seen_turns:
                        raise ValueError("duplicate turn")
                    seen_turns.add(turn_id)
                elif payload["turn_id"] != turn_id:
                    raise ValueError("mismatched turn")
                messages.append(
                    Message(f"{turn_id}:{role}", session_id, position, role, payload["content"])
                )
            if len(messages) % 2:
                raise ValueError("incomplete turn")
            return session, messages
        except (ValueError, KeyError, IndexError, TypeError):
            raise StorageError(
                f"会话 {session_id} 格式损坏或尾部轮次不完整；拒绝加载和追加，请先备份并修复文件。"
            ) from None

    def select_session(self, session_id: str) -> Session:
        if session_id == self._active_id:
            return self.get_session(session_id)
        self._path(session_id)
        lock = FileLock(self.root / ".locks" / f"{session_id}.lock", timeout=0)
        try:
            lock.acquire()
        except Timeout:
            raise StorageError(f"会话 {session_id} 已被其他进程占用。") from None
        try:
            session = self.get_session(session_id)
        except BaseException:
            lock.release()
            raise
        self.close()
        self._lock, self._active_id = lock, session_id
        return session

    def get_session(self, session_id: str) -> Session:
        return self._read(session_id)[0]

    def list_sessions(self) -> list[Session]:
        return [self.get_session(path.stem) for path in sorted(self.root.glob("*/*/*/*.jsonl"))]

    def load_messages(self, session_id: str) -> list[Message]:
        return self._read(session_id)[1]

    def append_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        self.select_session(session_id)
        messages = self.load_messages(session_id)
        used_ids = {message.message_id.split(":", 1)[0] for message in messages}
        turn_id = secrets.token_hex(4)
        while turn_id in used_ids:
            turn_id = secrets.token_hex(4)
        data = b"".join(
            record("message", {"turn_id": turn_id, "role": role, "content": content})
            for role, content in (("user", user_text), ("assistant", assistant_text))
        )
        with self._path(session_id).open("r+b") as file:
            file.seek(0, os.SEEK_END)
            start = file.tell()
            try:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                # 可捕获的失败尝试回滚；进程被强杀留下的半轮由加载校验拒绝。
                file.seek(start)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                raise

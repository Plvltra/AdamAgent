"""SQLite 会话存储；只保存完整成功轮次。"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self


@dataclass(frozen=True)
class Session:
    session_id: int
    title: str
    system_prompt: str


@dataclass(frozen=True)
class Message:
    message_id: int
    session_id: int
    position: int
    role: str
    content: str


class SessionNotFoundError(LookupError):
    """指定会话不存在。"""


class SessionStore:
    """持有一个 SQLite 连接，使用 with 或显式 close 释放。

    父目录由调用方创建；数据库异常原样向上传递，不自动重试。
    每个实例只在创建它的线程内使用。
    """

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, isolation_level="IMMEDIATE")
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL,
                        system_prompt TEXT NOT NULL
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        message_id INTEGER PRIMARY KEY,
                        session_id INTEGER NOT NULL REFERENCES sessions(session_id),
                        position INTEGER NOT NULL CHECK (position >= 0),
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        content TEXT NOT NULL,
                        UNIQUE (session_id, position)
                    )
                    """
                )
        except sqlite3.Error:
            self.close()
            raise

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
        self._connection.close()

    def create_session(self, title: str, system_prompt: str) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO sessions (title, system_prompt) VALUES (?, ?)",
                (title, system_prompt),
            )
            session_id = cursor.lastrowid
            if session_id is None:
                raise RuntimeError("创建会话后未取得 ID。")
        return session_id

    def get_session(self, session_id: int) -> Session:
        row = self._connection.execute(
            "SELECT session_id, title, system_prompt FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"会话 {session_id} 不存在。")
        return Session(*row)

    def list_sessions(self) -> list[Session]:
        rows = self._connection.execute(
            "SELECT session_id, title, system_prompt FROM sessions ORDER BY session_id"
        ).fetchall()
        return [Session(*row) for row in rows]

    def load_messages(self, session_id: int) -> list[Message]:
        self.get_session(session_id)
        rows = self._connection.execute(
            """
            SELECT message_id, session_id, position, role, content
            FROM messages WHERE session_id = ? ORDER BY position
            """,
            (session_id,),
        ).fetchall()
        return [Message(*row) for row in rows]

    def append_turn(self, session_id: int, user_text: str, assistant_text: str) -> None:
        """两条消息一起提交；任意一次插入失败，整个轮次回滚。"""
        with self._connection:
            # 在读取下一个位置前取得写锁，防止不同连接分配相同位置。
            self._connection.execute("BEGIN IMMEDIATE")
            self.get_session(session_id)
            position = self._connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            self._connection.executemany(
                "INSERT INTO messages (session_id, position, role, content) VALUES (?, ?, ?, ?)",
                [
                    (session_id, position, "user", user_text),
                    (session_id, position + 1, "assistant", assistant_text),
                ],
            )

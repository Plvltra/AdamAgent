"""每个会话一个 JSONL 文件；只加载完整的成功问答。"""

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from filelock import FileLock, Timeout

from adam.protocol import validate_turn


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
    raw: dict[str, Any]


class StorageError(OSError):
    """会话占用、格式损坏或写入失败。"""


class SessionNotFoundError(LookupError):
    """指定会话不存在。"""


def valid_session_id(value: str) -> bool:
    """判断输入是否为完整的 8 位小写十六进制会话 ID。"""
    return re.fullmatch(r"[0-9a-f]{8}", value) is not None


def record(kind: str, payload: dict[str, object]) -> bytes:
    """将事件类型和载荷编码为一行带 UTC 时间戳、换行符的 UTF-8 JSON。"""
    value = {"type": kind, "timestamp": datetime.now(UTC).isoformat(), "payload": payload}
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class SessionStore:
    """当前选中会话持有写锁；切换成功或关闭时释放，不跨线程共享实例。"""

    def __init__(self, path: Path) -> None:
        """建立会话根目录及锁目录；path 是按日期存储 JSONL 的根路径。"""
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
        """释放当前会话锁；可重复调用。"""
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self._active_id = None

    def create_session(self, title: str, system_prompt: str) -> str:
        """创建只有元信息的会话，返回 8 位短 ID；不自动选中。

        title 为展示名称，system_prompt 为本会话固定系统提示。
        ID 分配受跨进程锁保护；目录或写入失败抛出 OSError/StorageError。
        """
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
                    "version": 2,
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

    def select_session(self, session_id: str) -> Session:
        """获取指定会话的写锁并返回元信息；切换成功后释放旧锁。

        session_id 必须对应有效会话；不存在抛出 SessionNotFoundError，
        被占用或损坏抛出 StorageError，失败时保留原会话锁。
        """
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
        """读取 session_id 对应的元信息；验证完整文件，但不切换当前会话。"""
        return self._read(session_id)[0]

    def list_sessions(self) -> list[Session]:
        """按文件路径顺序返回全部会话元信息；遇到损坏文件时抛出 StorageError。"""
        return [self.get_session(path.stem) for path in sorted(self.root.glob("*/*/*/*.jsonl"))]

    def load_messages(self, session_id: str) -> list[Message]:
        """返回 session_id 的完整历史，消息按协议顺序排列。

        Message.raw 保留工具调用与推理字段；文件含残缺轮次时抛出
        StorageError，避免把不完整工具链发送给模型。
        """
        return self._read(session_id)[1]

    def usage(self, session_id: str) -> tuple[int, bool]:
        """返回 (已知累计 token 数, 是否存在未知用量或未结算请求)。

        统计包含失败轮次的请求，不只统计已保存的成功对话。
        """
        _, _, tokens, unknown = self._read(session_id)
        return tokens, unknown

    def start_request(self, session_id: str, model: str) -> str:
        """持久记录 session_id 的模型请求开始事件，返回用于结算的 request_id。"""
        request_id = secrets.token_hex(16)
        self._append(
            session_id, record("request_started", {"request_id": request_id, "model": model})
        )
        return request_id

    def finish_request(
        self, session_id: str, request_id: str, usage: tuple[int, int] | None
    ) -> None:
        """追加请求用量；request_id 对应 start_request 返回的标识。

        usage 是 (输入 token, 输出 token)；None 表示未知，不能按零处理。
        用量独立持久化，后续轮次失败也保留已消费记录。
        """
        self._append(
            session_id,
            record(
                "request_usage",
                {
                    "request_id": request_id,
                    "input_tokens": usage[0] if usage else None,
                    "output_tokens": usage[1] if usage else None,
                },
            ),
        )

    def append_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """将 user_text 和 assistant_text 作为一个完整普通问答保存到 session_id。"""
        self.append_messages(
            session_id,
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ],
        )

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """追加一轮完整协议消息及完成标记；messages 包括用户、工具轨迹与最终回答。

        校验失败抛出 ValueError；写入失败抛出 OSError，输入不会被修改。
        完成标记用于区分可恢复的完整历史和中断写入。
        """
        validate_turn(messages)
        turn_id = secrets.token_hex(16)
        data = b"".join(
            record("message", {"turn_id": turn_id, "message": message}) for message in messages
        )
        data += record("turn_completed", {"turn_id": turn_id})
        self._append(session_id, data)

    def append_command_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """独立保存命令开始、结束或拒绝事件，不依赖本轮模型最终回答成功。

        payload 包含 execution_id 和 tool_call_id；同一执行只能结束一次。
        写入失败必须停止执行流程，调用方不能将其当普通工具错误继续。
        """
        states = {item["execution_id"]: item for item in self.load_executions(session_id)}
        self._apply_command_event(states, kind, payload)
        self._append(session_id, record(kind, payload))

    def load_executions(self, session_id: str) -> list[dict[str, Any]]:
        """返回命令执行记录；只有开始事件的记录标为 unknown，不自动重放。

        同一会话持有写锁，已记录的 started/finished 事件不会混入模型消息。
        """
        self._read(session_id)
        states: dict[str, dict[str, Any]] = {}
        for line in self._path(session_id).read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry["type"].startswith("command_"):
                self._apply_command_event(states, entry["type"], entry["payload"])
        return list(states.values())

    def _path(self, session_id: str) -> Path:
        """校验短 ID 并查找唯一文件；缺失抛出 SessionNotFoundError，重复抛出 StorageError。"""
        if not valid_session_id(session_id):
            raise SessionNotFoundError("会话 ID 必须是 8 位小写十六进制字符。")
        paths = list(self.root.glob(f"*/*/*/{session_id}.jsonl"))
        if not paths:
            raise SessionNotFoundError(f"会话 {session_id} 不存在。")
        if len(paths) != 1:
            raise StorageError(f"会话 {session_id} 有重复文件，请检查日期目录。")
        return paths[0]

    def _read(self, session_id: str) -> tuple[Session, list[Message], int, bool]:
        """解析并验证事件日志，返回 (元信息, 完整消息, 已知 token 数, 用量未知标志)。

        request 事件独立累计；message 仅在 turn_completed 处提交。
        command 事件校验开始/结束配对，但不加入模型上下文或 token 统计。
        未结算请求保留为未知用量，残缺消息或非法事件拒绝加载。
        """
        try:
            raw = self._path(session_id).read_bytes()
            if not raw.endswith(b"\n"):
                raise ValueError("missing newline")
            entries = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("timestamp"), str)
                    or not isinstance(entry.get("payload"), dict)
                ):
                    raise ValueError("invalid record")
            meta = entries[0]
            data = meta["payload"]
            if (
                meta.get("type") != "session_meta"
                or type(data.get("version")) is not int
                or data["version"] != 2
            ):
                raise StorageError("不支持旧版或未知会话格式；请 /new 创建新会话。")
            if data.get("id") != session_id or not all(
                isinstance(data.get(key), str) for key in ("title", "system_prompt")
            ):
                raise ValueError("invalid metadata")
            session = Session(session_id, data["title"], data["system_prompt"])
            messages: list[Message] = []
            pending_messages: list[dict[str, Any]] = []
            turn_id = ""
            completed: set[str] = set()
            requests: dict[str, bool] = {}
            executions: dict[str, dict[str, Any]] = {}
            tokens = 0
            unknown = False
            for entry in entries[1:]:
                kind, payload = entry["type"], entry["payload"]
                if kind in {"command_started", "command_finished", "command_denied"}:
                    self._apply_command_event(executions, kind, payload)
                elif kind in {"request_started", "request_usage"}:
                    request_id = payload["request_id"]
                    if not isinstance(request_id, str) or not request_id:
                        raise ValueError("invalid request")
                    if kind == "request_started":
                        if request_id in requests:
                            raise ValueError("duplicate request")
                        requests[request_id] = False
                    else:
                        if request_id not in requests or requests[request_id]:
                            raise ValueError("unmatched usage")
                        requests[request_id] = True
                        inputs, outputs = payload["input_tokens"], payload["output_tokens"]
                        if inputs is None and outputs is None:
                            unknown = True
                        elif (
                            type(inputs) is int
                            and type(outputs) is int
                            and inputs >= 0
                            and outputs >= 0
                        ):
                            tokens += inputs + outputs
                        else:
                            raise ValueError("invalid usage")
                elif kind == "message":
                    current = payload["turn_id"]
                    if not isinstance(current, str) or not current or current in completed:
                        raise ValueError("invalid turn")
                    if pending_messages and current != turn_id:
                        raise ValueError("incomplete turn")
                    turn_id = current
                    message = payload["message"]
                    if not isinstance(message, dict):
                        raise ValueError("invalid message")
                    pending_messages.append(message)
                elif kind == "turn_completed":
                    if not pending_messages or payload["turn_id"] != turn_id:
                        raise ValueError("unmatched completion")
                    validate_turn(pending_messages)
                    for message in pending_messages:
                        messages.append(
                            Message(
                                f"{turn_id}:{len(messages)}",
                                session_id,
                                len(messages),
                                message["role"],
                                message.get("content") or "",
                                message,
                            )
                        )
                    completed.add(turn_id)
                    pending_messages = []
                else:
                    raise ValueError("unknown event")
            if pending_messages:
                raise ValueError("incomplete turn")
            return session, messages, tokens, unknown or not all(requests.values())
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise StorageError(
                f"会话 {session_id} 格式损坏或尾部轮次不完整；拒绝加载和追加。"
            ) from None

    @staticmethod
    def _apply_command_event(
        states: dict[str, dict[str, Any]], kind: str, payload: dict[str, Any]
    ) -> None:
        """验证命令事件配对并归并状态；缺少结束事件的执行保持 unknown。"""
        execution_id = payload.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("invalid execution id")
        if not isinstance(payload.get("tool_call_id"), str) or not payload["tool_call_id"]:
            raise ValueError("invalid tool call id")
        if kind in {"command_started", "command_denied"}:
            if execution_id in states:
                raise ValueError("duplicate execution")
            if (
                not isinstance(payload.get("argv"), list)
                or not all(isinstance(arg, str) for arg in payload["argv"])
                or not payload["argv"]
                or not isinstance(payload.get("cwd"), str)
            ):
                raise ValueError("invalid command")
            if kind == "command_denied" and payload.get("status") != "denied":
                raise ValueError("invalid denial")
            states[execution_id] = {
                **payload,
                "status": "unknown" if kind == "command_started" else "denied",
            }
        elif kind == "command_finished":
            previous = states.get(execution_id)
            if previous is None or previous["status"] != "unknown":
                raise ValueError("unmatched command finish")
            if payload["tool_call_id"] != previous["tool_call_id"]:
                raise ValueError("mismatched tool call")
            if payload.get("status") not in {
                "completed",
                "timed_out",
                "start_failed",
                "interrupted",
            }:
                raise ValueError("invalid command status")
            if payload.get("exit_code") is not None and type(payload["exit_code"]) is not int:
                raise ValueError("invalid exit code")
            states[execution_id] = {**previous, **payload}
        else:
            raise ValueError("unknown command event")

    def _append(self, session_id: str, data: bytes) -> None:
        """持锁追加并 flush/fsync；可捕获的写入异常尝试截断回原长度后重抛。

        data 是完整 JSONL 字节块。进程崩溃不保证回滚，恢复时由 _read
        检测残缺记录；已有损坏文件禁止继续追加。
        """
        self.select_session(session_id)
        self._read(session_id)
        with self._path(session_id).open("r+b") as file:
            file.seek(0, os.SEEK_END)
            start = file.tell()
            try:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                file.seek(start)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                raise

"""每个会话一个 JSONL 文件；只加载完整的成功问答。"""

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from filelock import FileLock, Timeout

from adam.errors import FailureCode, SessionNotFoundError, StorageError
from adam.execution_report import ExecutionReport, build_execution_report
from adam.history import HistoryTurn, Summary
from adam.models import Message, Session, valid_session_id
from adam.outcome import ExecutionOutcome
from adam.protocol import validate_turn
from adam.session_log import (
    SessionLog,
    UnsupportedLogVersionError,
    decode_records,
    parse_log,
    prepare_recovery,
)


@contextmanager
def _storage_io() -> Iterator[None]:
    """仅在存储操作边界转换系统 IO 错误，保留原因链；程序错误和用户中断继续传播。"""
    try:
        yield
    except OSError as error:
        raise StorageError("会话文件操作失败，请检查路径、权限和磁盘空间；未自动重试。") from error


def record(kind: str, payload: dict[str, object]) -> bytes:
    """将事件类型和载荷编码为一行带 UTC 时间戳、换行符的 UTF-8 JSON。"""
    value = {"type": kind, "timestamp": datetime.now(UTC).isoformat(), "payload": payload}
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class SessionStore:
    """当前选中会话持有写锁；切换成功或关闭时释放，不跨线程共享实例。

    文件与锁操作的系统错误在本模块转换为 StorageError；错误提示不泄露原始异常。
    日志关联校验失败仍抛出 ValueError，调用方不能把内部契约错误当作普通 IO 失败。
    """

    @_storage_io()
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

    @_storage_io()
    def close(self) -> None:
        """释放当前会话锁；可重复调用。"""
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self._active_id = None

    @_storage_io()
    def create_session(self, title: str, system_prompt: str) -> str:
        """创建只有元信息的会话，返回 8 位短 ID；不自动选中。

        title 为展示名称，system_prompt 为本会话固定系统提示。
        ID 分配受跨进程锁保护；目录或写入失败抛出 StorageError。
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
                    "version": 4,
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

    @_storage_io()
    def select_session(self, session_id: str) -> Session:
        """获取指定会话的写锁并返回元信息；切换成功后释放旧锁。

        session_id 必须对应有效会话；不存在抛出 SessionNotFoundError，
        新获锁后先备份并恢复遗留状态及残缺尾行，再读取历史，不自动重发。
        被占用、中间损坏或恢复失败抛出 StorageError，失败时保留原会话锁。
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
            self._recover_session(session_id)
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

    @_storage_io()
    def list_sessions(self) -> list[Session]:
        """按文件路径顺序返回全部会话元信息；遇到损坏文件时抛出 StorageError。"""
        return [self.get_session(path.stem) for path in sorted(self.root.glob("*/*/*/*.jsonl"))]

    def load_messages(self, session_id: str) -> list[Message]:
        """返回 session_id 的完整历史，消息按协议顺序排列。

        Message.raw 保留工具调用与推理字段；文件含残缺的成功消息块时抛出
        StorageError，避免把不完整工具链发送给模型。
        """
        return self._read(session_id)[1]

    def load_turns(self, session_id: str) -> list[HistoryTurn]:
        """按提交顺序返回完整成功轮次；不通过消息 ID 推测分组，不包含失败输入。"""
        return self._read_log(session_id).history_turns()

    def load_summary(self, session_id: str) -> Summary | None:
        """返回最近完整提交的摘要；失败或中断的候选摘要不生效。"""
        return self._read_log(session_id).compactions.summary

    def append_compaction_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """追加独立摘要操作；复用持锁、关联校验与失败回滚，不修改原消息。"""
        if kind not in {
            "compaction_started",
            "compaction_usage",
            "compaction_completed",
            "compaction_finished",
        }:
            raise ValueError("invalid compaction event")
        self._append(session_id, record(kind, payload))

    def usage(self, session_id: str) -> tuple[int, bool]:
        """返回 (已知累计 token 数, 是否存在未知用量或未结算请求)。

        统计包含失败轮次和独立摘要请求，不只统计已保存的成功对话。
        """
        _, _, tokens, unknown = self._read(session_id)
        return tokens, unknown

    def start_turn(self, session_id: str, user_text: str) -> str:
        """先保存用户输入并返回 turn_id；未完成轮次仅供诊断，不进入有效历史。"""
        turn_id = secrets.token_hex(16)
        self._append(
            session_id, record("turn_started", {"turn_id": turn_id, "user_text": user_text})
        )
        return turn_id

    def start_request(
        self, session_id: str, turn_id: str, model: str, *, context: dict[str, Any] | None = None
    ) -> str:
        """登记模型请求及可选选择依据，返回 request_id；context 不含完整请求副本。"""
        request_id = secrets.token_hex(16)
        self._append(
            session_id,
            record(
                "request_started",
                {
                    "turn_id": turn_id,
                    "request_id": request_id,
                    "model": model,
                    **({"context": context} if context is not None else {}),
                },
            ),
        )
        return request_id

    def finish_request(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        usage: tuple[int, int] | None,
        *,
        outcome: ExecutionOutcome | None = None,
    ) -> None:
        """在同一追加块内保存请求用量和终态；非法关联或重复结束抛出 ValueError。

        usage 是 (输入 token, 输出 token)；None 表示未知，不能按零处理。
        outcome 描述模型请求本身，与后续整轮结果独立；省略时按成功处理，耗时未知。
        有效响应缺少用量时请求仍可成功，不将用量未知当作请求失败。
        可捕获的写入失败回滚整个块；后续轮次失败不会删除已落盘的消费记录。
        """
        link = {"turn_id": turn_id, "request_id": request_id}
        data = record(
            "request_usage",
            {
                **link,
                "input_tokens": usage[0] if usage else None,
                "output_tokens": usage[1] if usage else None,
            },
        )
        data += record(
            "request_finished", {**link, **(outcome or ExecutionOutcome.success()).payload()}
        )
        self._append(session_id, data)

    def finish_turn(self, session_id: str, turn_id: str, outcome: ExecutionOutcome) -> None:
        """保存失败或中断的轮次终态，不提交消息；成功必须经过 append_messages。

        重复结束或未知轮次抛出 ValueError。存储失败时日志保留最后已落盘的状态，
        调用方应保留原始业务异常，不伪造已经保存的终态。
        """
        self._append(session_id, record("turn_finished", {"turn_id": turn_id, **outcome.payload()}))

    def append_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """保存一个普通问答；先登记输入，再提交消息，提交失败时诊断输入仍可能保留。"""
        messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
        validate_turn(messages)
        turn_id = self.start_turn(session_id, user_text)
        self.append_messages(session_id, turn_id, messages)

    def append_messages(
        self,
        session_id: str,
        turn_id: str,
        messages: list[dict[str, Any]],
        *,
        outcome: ExecutionOutcome | None = None,
    ) -> None:
        """向已开始的轮次追加完整消息与完成标记，成功后才能加载为有效历史。

        messages 包括原始用户输入、工具轨迹和最终回答，须与诊断记录一致。
        校验失败抛出 ValueError；写入失败抛出 StorageError，输入不会被修改。
        完成标记用于区分可恢复的完整历史和中断写入。outcome 必须为成功；
        省略时不猜测耗时。成功原因与完整消息同块落盘，不提前声明提交成功。
        """
        validate_turn(messages)
        data = b"".join(
            record("message", {"turn_id": turn_id, "message": message}) for message in messages
        )
        data += record(
            "turn_completed",
            {"turn_id": turn_id, **(outcome or ExecutionOutcome.success()).payload()},
        )
        self._append(session_id, data)

    def append_tool_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """保存工具开始或结果事件，失败轮次也保留轨迹；不加入有效聊天历史。

        payload 必须包含 turn_id、request_id 和 tool_call_id；开始事件带 name/arguments，
        结束事件带实际回传的 output。关联错误在写入前抛出 ValueError。
        """
        if kind not in {"tool_started", "tool_finished"}:
            raise ValueError("unknown tool event")
        self._append(session_id, record(kind, payload))

    def append_command_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """独立保存命令开始、结束或拒绝事件，不依赖本轮模型最终回答成功。

        payload 包含 turn_id、request_id、execution_id 和 tool_call_id；
        必须关联已开始的 run_command 工具，同一执行只能结束一次。
        写入失败必须停止执行流程，调用方不能将其当普通工具错误继续。
        """
        if kind not in {"command_started", "command_finished", "command_denied"}:
            raise ValueError("unknown command event")
        self._append(session_id, record(kind, payload))

    def append_edit_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        """保存编辑计划/写入开始/结果，关联父轮次、请求和工具；失败必须停止执行。"""
        if kind not in {"edit_planned", "edit_started", "edit_finished"}:
            raise ValueError("unknown edit event")
        self._append(session_id, record(kind, payload))

    def execution_report(self, session_id: str, turn_id: str) -> ExecutionReport:
        """从同一份日志快照生成指定轮次的执行事实；读取失败遵循 StorageError 契约。"""
        return build_execution_report(self._read_log(session_id), turn_id)

    def load_edits(self, session_id: str) -> list[dict[str, Any]]:
        """返回编辑证据；unknown 不能因恢复而被当成未执行或成功。"""
        return list(self._read_log(session_id).edits.operations.values())

    def load_executions(self, session_id: str) -> list[dict[str, Any]]:
        """返回命令执行记录；只有开始事件的记录标为 unknown，不自动重放。

        同一会话持有写锁，已记录的 started/finished 事件不会混入模型消息。
        """
        return list(self._read_log(session_id).executions.values())

    def load_events(self, session_id: str) -> list[dict[str, Any]]:
        """返回按落盘顺序排列的完整事件，供诊断使用；损坏文件仍拒绝读取。"""
        return self._read_log(session_id).events

    def _recover_session(self, session_id: str) -> None:
        """仅在新取得写锁后恢复；先持久备份，再以同目录临时文件原子替换。

        恢复前校验全部完整记录。备份或替换失败时不改原文件；已生成备份保留。
        原子替换防止读者看到半份修复结果，但不承诺断电时的目录项持久性。
        """
        path = self._path(session_id)
        original = path.read_bytes()
        try:
            complete, log = prepare_recovery(original, session_id)
            targets = log.recovery_targets()
            if complete == original and not any(targets.values()):
                return
            backup = path.with_name(path.name + "." + secrets.token_hex(8) + ".bak")
            outcome = ExecutionOutcome(
                "interrupted",
                FailureCode.RESTART_RECOVERY.value,
                message="上次缺少完整结束记录；恢复时标记中断，未重发请求或重跑工具。",
            )
            repaired = complete + record(
                "session_recovered",
                {
                    **targets,
                    **outcome.payload(),
                    "tail_repaired": complete != original,
                    "backup_file": backup.name,
                },
            )
            parse_log(repaired, session_id)
        except UnsupportedLogVersionError:
            raise StorageError("不支持旧版或未知会话格式；请 /new 创建新会话。") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise StorageError("会话格式不受支持或完整记录已损坏；未自动修复。") from None
        with backup.open("xb") as file:
            file.write(original)
            file.flush()
            os.fsync(file.fileno())
        temporary = backup.with_suffix(".recovery.tmp")
        try:
            with temporary.open("xb") as file:
                file.write(repaired)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 替换失败时保留原日志和备份；临时文件清理失败不遮蔽原始异常。
                pass

    @_storage_io()
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
        """返回元信息、成功历史、已知 token 数及未知用量标志；诊断输入不进入消息。"""
        log = self._read_log(session_id)
        session = Session(session_id, log.metadata["title"], log.metadata["system_prompt"])
        messages = [
            Message(
                f"{turn_id}:{position}",
                session_id,
                position,
                message["role"],
                message.get("content") or "",
                message,
            )
            for position, (turn_id, message) in enumerate(log.messages)
        ]
        return session, messages, log.tokens, log.unknown_usage

    @_storage_io()
    def _read_log(self, session_id: str) -> SessionLog:
        """解析并校验事件关联；请求独立计费，成功消息只在 turn_completed 处提交。

        未结算请求保持未知用量，工具和命令事件不直接加入模型历史。
        诊断轮次可以未完成；残缺行、半个成功消息块或非法关联拒绝加载。
        """
        try:
            return parse_log(self._path(session_id).read_bytes(), session_id)
        except UnsupportedLogVersionError:
            raise StorageError("不支持旧版或未知会话格式；请 /new 创建新会话。") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise StorageError(
                f"会话 {session_id} 格式损坏或尾部轮次不完整；拒绝加载和追加。"
            ) from None

    @_storage_io()
    def _append(self, session_id: str, data: bytes) -> None:
        """持锁追加并 flush/fsync；可捕获的写入异常尝试截断回原长度后重抛。

        data 是完整 JSONL 字节块。进程崩溃不保证回滚，恢复时由 _read
        检测残缺记录；系统 IO 错误经 _storage_io 转为 StorageError。
        已有损坏文件禁止继续追加。新事件关联在持锁后先验证，
        校验失败抛出 ValueError 且不触碰文件，不能污染原有日志。
        """
        self.select_session(session_id)
        log = self._read_log(session_id)
        log.extend(decode_records(data))
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

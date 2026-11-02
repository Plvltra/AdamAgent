"""运行中用户输入与独立留痕；输入线程不访问会话JSONL或模型上下文。"""

import json
import os
import re
import secrets
from dataclasses import asdict
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from adam.application.event_queue import EventQueue
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError, StorageError
from adam.contracts.events import EventKind, EventReceipt, InputEvent
from adam.contracts.models import valid_session_id


class RunningInputs:
    """每次begin绑定一个轮次，finish后拒绝投递；成功回执表示接受记录已落盘。"""

    def __init__(self, root: Path, cancel: CancellationToken) -> None:
        self.root, self.cancel = root.absolute(), cancel
        self._lock, self._journal_lock = RLock(), Lock()
        self._queue: EventQueue | None = None
        self._path: Path | None = None
        self._record: dict[str, Any] = {}

    def begin(self, session_id: str | None, turn_id: str) -> None:
        """仅主执行线程在登记轮次后调用；新轮次重置停止信号，不复用旧队列。"""
        with self._lock:
            if self._queue is not None:
                raise AgentError("已有运行中的输入队列。")
            if (
                session_id is None
                or not valid_session_id(session_id)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", turn_id)
            ):
                raise AgentError("运行输入身份无效。")
            self.cancel.reset()
            self._path = self.root / session_id / f"{turn_id}.json"
            self._record = {
                "session_id": session_id,
                "turn_id": turn_id,
                "status": "running",
                "events": [],
            }
            self._save()
            self._queue = EventQueue(
                turn_id, max_events=64, on_accept=self._accept, on_ack=self._ack
            )

    def queue(self, turn_id: str) -> EventQueue | None:
        """只向原轮次暴露队列，跨运行输入不能混入后续任务。"""
        with self._lock:
            if self._queue is not None and self._queue.task_id == turn_id:
                return self._queue
            return None

    def submit(self, text: str, event_id: str | None = None) -> EventReceipt:
        """终端用户补充先持久接受后返回；满、关闭或重复ID冲突均明确报错。"""
        with self._lock:
            if self._queue is None:
                raise AgentError("当前没有运行中的任务，请作为下一轮发送。")
            try:
                return self._queue.put(
                    EventKind.USER_INPUT, "terminal", event_id or secrets.token_hex(8), text
                )
            except ValueError as error:
                raise AgentError(f"补充输入未接受：{error}") from error
            except StorageError:
                self.cancel.cancel()
                raise

    def stop(self) -> None:
        """停止信号不排队，直接打断共享令牌控制的请求及执行边界。"""
        with self._lock:
            if self._queue is None:
                raise AgentError("当前没有运行中的任务。")
            self.cancel.cancel()

    def check(self) -> None:
        """仅活轮次检查停止信号，已结束轮次不影响终端等待下一次输入。"""
        with self._lock:
            if self._queue is not None:
                self.cancel.check()

    def finish(self, succeeded: bool) -> None:
        """关闭输入并保存轮次结果；仍在队列中的补充标记未处理，不重发。"""
        with self._lock:
            if self._queue is None:
                return
            self._queue.close()
            try:
                with self._journal_lock:
                    self._record["status"] = "completed" if succeeded else "interrupted"
                    for event in self._record["events"]:
                        if event["state"] == "accepted":
                            event["state"] = "unprocessed"
                    self._save()
            finally:
                self._queue = None

    def _accept(self, event: InputEvent) -> None:
        with self._journal_lock:
            self._record["events"].append({**asdict(event), "state": "accepted"})
            try:
                self._save()
            except BaseException:
                self._record["events"].pop()
                raise

    def _ack(self, event: InputEvent) -> None:
        with self._journal_lock:
            self._record["events"][event.sequence - 1]["state"] = "in_context"
            try:
                self._save()
            except BaseException:
                self._record["events"][event.sequence - 1]["state"] = "accepted"
                raise

    def _save(self) -> None:
        """临时文件fsync后替换；失败停止本轮，不把未保存输入当成已接受。"""
        assert self._path is not None
        _write_record(self._path, self._record)


def recover_inputs(root: Path, session_id: str, completed_turns: set[str]) -> int:
    """宿主取得主会话锁后恢复遗留输入；成功事实来自已提交轮次，不按输入标记猜测。

    只更新running记录，保留正文、来源、序号和in_context；未消费内容标为unprocessed。
    不重建队列或请求模型，残留tmp不参与恢复。损坏记录明确报错，原文件保留。
    """
    if not valid_session_id(session_id):
        raise StorageError("输入恢复会话身份无效。")
    directory = root.absolute() / session_id
    try:
        if directory.resolve() != directory:
            raise ValueError("input storage link")
        if not directory.exists():
            return 0
        changed = 0
        for count, path in enumerate(directory.glob("*.json"), 1):
            if count > 4096 or path.resolve() != path or path.stat().st_size > 4_000_000:
                raise ValueError("input recovery limit or link")
            record = json.loads(path.read_text(encoding="utf-8"))
            _validate_record(record, session_id, path.stem)
            if record["status"] != "running":
                continue
            record["status"] = "completed" if path.stem in completed_turns else "interrupted"
            record["recovery_reason"] = "依据已提交轮次恢复；未处理输入未自动重发。"
            for event in record["events"]:
                if event["state"] == "accepted":
                    event["state"] = "unprocessed"
            _write_record(path, record)
            changed += 1
        return changed
    except (OSError, ValueError, TypeError) as error:
        raise StorageError("补充输入记录损坏或不可访问，未自动重发。") from error


def _validate_record(record: object, session_id: str, turn_id: str) -> None:
    """恢复只接受本会话的有界顺序记录，不把损坏文件当成可重发任务。"""
    if (
        not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", turn_id)
        or not isinstance(record, dict)
        or record.get("session_id") != session_id
        or record.get("turn_id") != turn_id
        or record.get("status") not in {"running", "completed", "interrupted"}
        or not isinstance(record.get("events"), list)
        or len(record["events"]) > 64
    ):
        raise ValueError("invalid input record")
    seen = set()
    for sequence, event in enumerate(record["events"], 1):
        if (
            not isinstance(event, dict)
            or type(event.get("sequence")) is not int
            or event["sequence"] != sequence
            or event.get("task_id") != turn_id
            or event.get("kind") != "user_input"
            or event.get("source") != "terminal"
            or event.get("state") not in {"accepted", "in_context", "unprocessed"}
            or not isinstance(event.get("text"), str)
            or not 1 <= len(event["text"]) <= 8192
            or not isinstance(event.get("event_id"), str)
            or not 1 <= len(event["event_id"]) <= 128
        ):
            raise ValueError("invalid input event")
        if event["event_id"] in seen:
            raise ValueError("duplicate input event")
        seen.add(event["event_id"])


def _write_record(path: Path, record: dict[str, Any]) -> None:
    """临时文件fsync后替换；写入失败保留原文件，临时文件不会被恢复器读取。"""
    temporary = path.with_suffix(".tmp")
    try:
        if path.resolve() != path or temporary.resolve() != temporary:
            raise ValueError("input storage link")
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as error:
        raise StorageError("补充输入记录保存失败，已停止，未自动重发。") from error

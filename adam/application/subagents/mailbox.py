"""单次子运行的双向消息快照；父子线程共用实例，重启只读事实而不重发。"""

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from adam.application.event_queue import EventQueue
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.events import EventKind, InputEvent
from adam.contracts.models import valid_session_id


class RunMailbox:
    """每运行各64条入站/进展消息，队列最多32条待消费；持久接受先于成功回执。"""

    def __init__(self, root: Path, agent_id: str, run_id: str, *, create: bool = True) -> None:
        if not valid_session_id(agent_id) or not re.fullmatch(r"[0-9a-f]{16}", run_id):
            raise ValueError("invalid mailbox identity")
        self.path = root.absolute() / agent_id / f"{run_id}.json"
        self._lock = RLock()
        self._source: dict[str, str] = {}
        self._data: dict[str, Any] = {
            "agent_id": agent_id,
            "run_id": run_id,
            "status": "running",
            "incoming": [],
            "progress": [],
        }
        self.queue: EventQueue | None = None
        if create:
            if self.path.exists():
                raise StorageError("子运行邮箱已经存在，未覆盖。")
            self._save()
            self.queue = EventQueue(
                run_id,
                capacity=32,
                max_events=64,
                max_chars=8192,
                on_accept=self._accept,
                on_ack=self._ack,
            )
        else:
            self._read(agent_id, run_id)

    def receive(self, message_id: str, text: str, source: dict[str, str]) -> dict[str, Any]:
        """父线程投递，记录原请求来源；同ID同内容去重，已关闭/恢复邮箱拒绝接收。"""
        if self.queue is None:
            raise ToolError("恢复的邮箱不能重发消息。")
        self._source = dict(source)
        try:
            receipt = self.queue.put(EventKind.AGENT_MESSAGE, "parent", message_id, text)
        except ValueError as error:
            raise ToolError(f"子任务补充未接受：{error}") from error
        return {"sequence": receipt.sequence, "duplicate": receipt.duplicate, "status": "accepted"}

    def publish(self, message_id: str, text: str) -> dict[str, Any]:
        """子线程报告进展；这只是模型声明，不验证任务成功，不改变最终结果收集状态。"""
        if (
            not isinstance(message_id, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", message_id)
            or not isinstance(text, str)
            or not 1 <= len(text) <= 2048
        ):
            raise ToolError("进展ID无效或正文超过2048字符。")
        with self._lock:
            if self._data["status"] != "running" or self.queue is None:
                raise ToolError("子运行已经结束，未发布进展。")
            rows = self._data["progress"]
            for row in rows:
                if row["message_id"] == message_id:
                    if row["text"] != text:
                        raise ToolError("进展message_id不能替换原正文。")
                    return {"sequence": row["sequence"], "duplicate": True}
            if len(rows) >= 64:
                raise ToolError("本运行进展已达64条上限。")
            row = {
                "message_id": message_id,
                "sequence": len(rows) + 1,
                "text": text,
                "state": "reported",
            }
            rows.append(row)
            try:
                self._save()
            except BaseException:
                rows.pop()
                raise
            return {"sequence": row["sequence"], "duplicate": False}

    def collect(self, cursor: int = 0, *, max_chars: int = 8000) -> dict[str, Any]:
        """父线程每次读取一条进展；cursor是已读序号，collected不等于父模型已处理。"""
        with self._lock:
            rows = self._data["progress"]
            if type(cursor) is not int or not 0 <= cursor <= len(rows):
                raise ToolError("进展游标无效。")
            selected = []
            if cursor < len(rows):
                row = rows[cursor]
                old = row["state"]
                row["state"] = "collected"
                try:
                    self._save()
                except BaseException:
                    row["state"] = old
                    raise
                limit = max(0, (max_chars - 600) // 6)
                selected = [
                    {**row, "text": row["text"][:limit], "truncated": len(row["text"]) > limit}
                ]
            next_cursor = cursor + len(selected)
            return {
                "agent_id": self._data["agent_id"],
                "run_id": self._data["run_id"],
                "status": self._data["status"],
                "progress": selected,
                "next_cursor": next_cursor,
                "has_more": next_cursor < len(rows),
                "note": "子Agent自报进展，不代表任务成功；最终仍需wait_agents。",
            }

    def finish(self, status: str) -> None:
        """关闭邮箱并结算未消费消息；以调度索引/子历史给出的状态为准，不重新入队。"""
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid mailbox terminal status")
        if self.queue is not None:
            self.queue.close()
        with self._lock:
            if self._data["status"] == status:
                return
            self._data["status"] = status
            for row in self._data["incoming"]:
                if row["state"] == "accepted":
                    row["state"] = "unprocessed"
            self._save()

    def _accept(self, event: InputEvent) -> None:
        with self._lock:
            self._data["incoming"].append(
                {**asdict(event), "state": "accepted", "parent_source": dict(self._source)}
            )
            try:
                self._save()
            except BaseException:
                self._data["incoming"].pop()
                raise

    def _ack(self, event: InputEvent) -> None:
        with self._lock:
            row = self._data["incoming"][event.sequence - 1]
            row["state"] = "in_context"
            try:
                self._save()
            except BaseException:
                row["state"] = "accepted"
                raise

    def _read(self, agent_id: str, run_id: str) -> None:
        """读取有界身份快照，不恢复执行队列。"""
        try:
            if self.path.resolve() != self.path or self.path.stat().st_size > 4_000_000:
                raise ValueError("invalid mailbox path or size")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                data.get("agent_id") != agent_id
                or data.get("run_id") != run_id
                or data.get("status") not in {"running", "completed", "failed", "interrupted"}
            ):
                raise ValueError("invalid mailbox owner")
            for field in ("incoming", "progress"):
                rows = data.get(field)
                if not isinstance(rows, list) or len(rows) > 64:
                    raise ValueError("invalid mailbox count")
                seen: set[str] = set()
                for sequence, row in enumerate(rows, 1):
                    states = (
                        {"accepted", "in_context", "unprocessed"}
                        if field == "incoming"
                        else {"reported", "collected"}
                    )
                    if (
                        not isinstance(row, dict)
                        or type(row.get("sequence")) is not int
                        or row["sequence"] != sequence
                        or row.get("state") not in states
                        or not isinstance(row.get("text"), str)
                        or len(row["text"]) > (8192 if field == "incoming" else 2048)
                    ):
                        raise ValueError("invalid mailbox event")
                    identifier = row.get("event_id" if field == "incoming" else "message_id")
                    if (
                        not isinstance(identifier, str)
                        or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", identifier)
                        or identifier in seen
                    ):
                        raise ValueError("invalid or duplicate mailbox message ID")
                    seen.add(identifier)
                    if field == "incoming" and (
                        row.get("task_id") != run_id
                        or row.get("source") != "parent"
                        or row.get("kind") != "agent_message"
                        or not isinstance(row.get("parent_source"), dict)
                    ):
                        raise ValueError("invalid mailbox message owner")
            self._data = data
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise StorageError("子运行消息记录损坏或不可读取，未重发。") from error

    def _save(self) -> None:
        """父子线程串行写同一快照，fsync后替换；失败不能作为普通工具重试。"""
        temporary = self.path.with_suffix(".tmp")
        try:
            if self.path.resolve() != self.path or temporary.resolve() != temporary:
                raise ValueError("mailbox path redirects")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self._data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as error:
            raise StorageError("子运行消息保存失败，未自动重发。") from error

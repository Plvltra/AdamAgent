"""终端的会话归属与持久事实；重启只恢复记录，不按PID重连或重发输入。"""

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.contracts.errors import CommandLogError, ToolError
from adam.contracts.models import valid_session_id
from adam.tool_calls.terminal_sessions import TerminalSessions


class TerminalService:
    """协调线程持有独立会话锁；启动/输入意图先落盘，失败不报告已经完成。"""

    def __init__(self, root: Path, terminals: TerminalSessions) -> None:
        self.root, self.terminals = root.absolute(), terminals
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._locks: dict[str, FileLock] = {}
        self._live: set[tuple[str, str]] = set()

    def start(self, owner: str, args: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
        """先保存启动意图，再申请批准；启动后落盘失败会回收该终端。"""
        records = self._load(owner)
        if len(records) >= 32:
            raise ToolError("本会话终端记录已达32条。")
        identity = secrets.token_hex(8)
        record = {
            "terminal_id": identity,
            "owner": owner,
            "status": "starting",
            "source": dict(source),
            "inputs": [],
        }
        self._save(owner, record)
        records[identity] = record
        try:
            result = self.terminals.start(owner, args, identifier=identity)
        except BaseException:
            record["status"] = "start_failed"
            self._save(owner, record)
            raise
        if result["status"] == "denied":
            record["status"] = "denied"
            self._save(owner, record)
            return {**result, "terminal_id": identity}
        self._live.add((owner, identity))
        try:
            return self.query(owner, identity)
        except BaseException:
            self.terminals.stop(owner, identity)
            raise

    def send(
        self, owner: str, identity: str, text: str, source: dict[str, str], *, eof: bool = False
    ) -> dict[str, Any]:
        """每次输入意图保存来源和正文后再批准；accepted不代表程序消费或执行成功。"""
        record = self._get(owner, identity)
        if (owner, identity) not in self._live:
            raise ToolError("旧终端不可重连，请明确创建新终端。")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 8192 or type(eof) is not bool:
            raise ToolError("输入文本最多8192字节，eof须为布尔值。")
        if len(record["inputs"]) >= 64:
            raise ToolError("本终端已达到64次输入上限。")
        self.query(owner, identity)
        item = {
            "input_id": secrets.token_hex(8),
            "source": dict(source),
            "text": text,
            "eof": eof,
            "status": "requested",
        }
        record["inputs"].append(item)
        self._save(owner, record)
        try:
            result = self.terminals.send(owner, identity, text, eof=eof)
        except BaseException:
            item["status"] = "unknown"
            self._save(owner, record)
            raise
        item["status"] = "accepted" if result["ok"] else "denied"
        try:
            self._save(owner, record)
        except BaseException:
            self.terminals.stop(owner, identity)
            raise
        return {**result, "input_id": item["input_id"]}

    def query(self, owner: str, identity: str, **cursors: int) -> dict[str, Any]:
        """保存当前有界输出观察；旧记录返回restored，不伪造当前进程状态。"""
        record = self._get(owner, identity)
        if (owner, identity) not in self._live:
            return {
                "ok": True,
                "terminal_id": identity,
                "status": record["status"],
                "restored": True,
                "observation": record.get("observation"),
                "note": "只保留旧观察；没有重连、重发或按PID终止进程。",
            }
        result = self.terminals.read(owner, identity, **cursors)
        record.update(status=result["status"], observation=result)
        if record["inputs"] and record["inputs"][-1]["status"] in {"accepted", "pending"}:
            record["inputs"][-1]["status"] = result["input_status"]
        self._save(owner, record)
        return result

    def stop(self, owner: str, identity: str) -> dict[str, Any]:
        """只停止本宿主仍持有的终端；旧记录不按PID采取动作。"""
        self._get(owner, identity)
        if (owner, identity) in self._live:
            self.terminals.stop(owner, identity)
        return self.query(owner, identity)

    def cancel_owner(self, owner: str | None) -> None:
        """用户取消仅回收当前会话终端，不影响其他会话。"""
        for sid, identity in tuple(self._live):
            if sid == owner:
                self.stop(sid, identity)

    def recover(self, owner: str) -> None:
        """取得主会话锁后检查已有目录；无终端记录时不创建文件。"""
        if valid_session_id(owner) and (self.root / owner).exists():
            self._load(owner)

    def close(self) -> None:
        """回收活进程后保存最后观察，并始终释放附属文件锁。"""
        self.terminals.close()
        try:
            for owner, identity in self._live:
                self.query(owner, identity)
        finally:
            for lock in self._locks.values():
                lock.release()
            self._locks.clear()

    def _get(self, owner: str, identity: str) -> dict[str, Any]:
        records = self._load(owner)
        if identity not in records:
            raise ToolError("终端不存在或不属于当前会话。")
        return records[identity]

    def _load(self, owner: str) -> dict[str, dict[str, Any]]:
        if not valid_session_id(owner):
            raise ToolError("终端会话身份无效。")
        if owner in self._records:
            return self._records[owner]
        lock = None
        try:
            directory = self._path(owner)
            directory.mkdir(parents=True, exist_ok=True)
            lock = FileLock(self._path(owner, "terminals.lock"), timeout=0)
            lock.acquire()
            records = {}
            for path in directory.glob("*.json"):
                if len(records) >= 32 or not re.fullmatch(r"[0-9a-f]{16}", path.stem):
                    raise ValueError("invalid terminal files")
                path = self._path(owner, path.name)
                if path.stat().st_size > 4_000_000:
                    raise ValueError("terminal record too large")
                record = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(record, dict)
                    or record.get("owner") != owner
                    or record.get("terminal_id") != path.stem
                    or not isinstance(record.get("inputs"), list)
                    or len(record["inputs"]) > 64
                ):
                    raise ValueError("invalid terminal identity")
                if record.get("status") in {"starting", "running"}:
                    record["status"] = "interrupted"
                    for item in record["inputs"]:
                        if item["status"] in {"requested", "accepted", "pending"}:
                            item["status"] = "unknown"
                    self._save(owner, record)
                records[path.stem] = record
            self._locks[owner], self._records[owner] = lock, records
            return records
        except Timeout:
            raise ToolError("该会话终端仍由另一宿主管理。") from None
        except (OSError, ValueError, TypeError, KeyError) as error:
            if lock:
                lock.release()
            raise CommandLogError("终端记录无法恢复，未执行任何输入。") from error
        except BaseException:
            if lock:
                lock.release()
            raise

    def _path(self, owner: str, name: str = "") -> Path:
        path = self.root / owner / name
        if path.resolve() != path:
            raise ValueError("terminal storage redirects through a link")
        return path

    def _save(self, owner: str, record: dict[str, Any]) -> None:
        """意图/观察原子替换；保存失败抛存储错误，禁止降级为可重试工具失败。"""
        try:
            target = self._path(owner, record["terminal_id"] + ".json")
            temporary = self._path(owner, record["terminal_id"] + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except (OSError, ValueError) as error:
            raise CommandLogError("终端记录保存失败，输入结果可能未知；未自动重发。") from error

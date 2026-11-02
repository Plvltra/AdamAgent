"""后台任务的持久登记与所有权；所有磁盘操作都在调用线程执行。"""

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.contracts.errors import CommandLogError, ToolError
from adam.contracts.models import valid_session_id
from adam.tool_calls.background_jobs import BackgroundJobs


class BackgroundService:
    """持有每个已访问会话的任务锁，直到关闭；恢复不按旧 PID 杀进程或重跑。

    仅保存调用时观察到的输出；崩溃后的未观察输出未知。侧文件不是进程守护服务。
    """

    def __init__(self, root: Path, jobs: BackgroundJobs) -> None:
        self.root, self.jobs = root.absolute(), jobs
        self._locks: dict[str, FileLock] = {}
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._live: dict[tuple[str, str], str] = {}

    def start(self, owner: str, args: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
        """先保存启动意图，再申请批准并启动；后续保存失败取消进程且停止本轮。"""
        records = self._load(owner)
        if len(records) >= 32:
            raise ToolError("本会话后台任务记录已达32条上限。")
        task_id = secrets.token_hex(8)
        record = {
            "task_id": task_id,
            "owner": owner,
            "status": "starting",
            "source": dict(source),
            "argv": args.get("argv"),
            "cwd": args.get("cwd", "."),
        }
        self._save(owner, record)
        records[task_id] = record
        try:
            result = self.jobs.start(owner, args)
        except BaseException:
            record["status"] = "start_failed"
            self._save(owner, record)
            raise
        if result["status"] == "denied":
            record["status"] = "denied"
            self._save(owner, record)
            return {"ok": False, "task_id": task_id, "status": "denied"}
        live_id = result["task_id"]
        self._live[owner, task_id] = live_id
        try:
            return self.query(owner, task_id)
        except BaseException:
            self.jobs.cancel(owner, live_id)
            self.jobs.wait(owner, live_id, 10)
            raise

    def query(
        self,
        owner: str,
        task_id: str,
        *,
        action: str = "read",
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        seconds: int = 1,
        max_bytes: int = 512,
    ) -> dict[str, Any]:
        """查询真实任务并保存观察；恢复的旧任务只返回上次快照，明确禁止继续执行。"""
        if (
            action not in {"read", "wait", "cancel"}
            or type(seconds) is not int
            or not 0 <= seconds <= 10
        ):
            raise ToolError("后台动作须为read/wait/cancel，等待时间为0～10秒。")
        records = self._load(owner)
        if task_id not in records:
            raise ToolError("后台任务不存在或不属于当前会话。")
        record = records[task_id]
        live_id = self._live.get((owner, task_id))
        if live_id is None:
            return {
                "ok": True,
                **record,
                "restored": True,
                "output_note": "仅有上次保存快照；未观察输出未知，不自动重跑。",
            }
        if action == "cancel":
            self.jobs.cancel(owner, live_id)
        if action in {"wait", "cancel"}:
            self.jobs.wait(owner, live_id, seconds)
        snapshot = self.jobs.snapshot(owner, live_id)
        record.update(snapshot, task_id=task_id)
        self._save(owner, record)
        page = self.jobs.read_output(
            owner,
            live_id,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
        )
        return {
            **page,
            "status": snapshot["status"],
            "task_id": task_id,
            "exit_code": snapshot["exit_code"],
            "elapsed_seconds": snapshot["elapsed_seconds"],
        }

    def recover(self, owner: str) -> None:
        """恢复会话时仅检查已有任务目录，无任务则不新建文件。"""
        if valid_session_id(owner) and self._path(owner).exists():
            self._load(owner)

    def cancel_owner(self, owner: str | None) -> None:
        """取消当前会话的全部本机任务，保留其他会话后台执行。"""
        if owner is None:
            return
        selected = [(task, live) for (sid, task), live in self._live.items() if sid == owner]
        for _, live in selected:
            self.jobs.cancel(owner, live)
        for task, _ in selected:
            self.query(owner, task, action="wait", seconds=10)

    def close(self) -> None:
        """先回收所有后台进程并保存终态，再释放任务锁；即使保存失败也释放锁。"""
        self.jobs.close()
        try:
            for (owner, task_id), live_id in self._live.items():
                record = self._records[owner][task_id]
                record.update(self.jobs.snapshot(owner, live_id), task_id=task_id)
                self._save(owner, record)
        finally:
            for lock in self._locks.values():
                lock.release()
            self._locks.clear()

    def _load(self, owner: str) -> dict[str, dict[str, Any]]:
        """获得独立会话任务锁后才恢复遗留记录，另一宿主仍持锁时拒绝恢复。"""
        if not valid_session_id(owner):
            raise ToolError("后台任务会话身份无效。")
        if owner in self._records:
            return self._records[owner]
        lock = None
        try:
            directory = self._path(owner)
            directory.mkdir(parents=True, exist_ok=True)
            lock = FileLock(self._path(owner, "tasks.lock"), timeout=0)
            lock.acquire()
            records = {}
            for path in directory.glob("*.json"):
                if len(records) >= 32 or not re.fullmatch(r"[0-9a-f]{16}", path.stem):
                    raise ValueError("invalid task files")
                path = self._path(owner, path.name)
                if path.stat().st_size > 2_000_000:
                    raise ValueError("oversized task record")
                record = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(record, dict)
                    or record.get("owner") != owner
                    or record.get("task_id") != path.stem
                ):
                    raise ValueError("invalid task owner")
                if record.get("status") in {"starting", "running"}:
                    record.update(
                        status="interrupted",
                        exit_code=None,
                        recovery_reason="原宿主退出，进程结果未知；未重跑或按旧PID终止进程。",
                    )
                    self._save(owner, record)
                records[path.stem] = record
            self._locks[owner], self._records[owner] = lock, records
            return records
        except Timeout:
            raise ToolError("该会话后台任务仍由另一个终端管理，未恢复或重跑。") from None
        except (OSError, ValueError) as error:
            if lock is not None:
                lock.release()
            raise CommandLogError("后台任务记录读取失败，未执行命令。") from error
        except BaseException:
            if lock is not None:
                lock.release()
            raise

    def _path(self, owner: str, name: str = "") -> Path:
        path = self.root / owner / name
        if path.resolve() != path:
            raise ValueError("background storage redirects through a link")
        return path

    def _save(self, owner: str, record: dict[str, Any]) -> None:
        """写临时文件并fsync后原子替换；日志故障不得降级成可重试工具错误。"""
        temporary = None
        try:
            target = self._path(owner, f"{record['task_id']}.json")
            temporary = self._path(owner, f"{record['task_id']}.{secrets.token_hex(4)}.tmp")
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except (OSError, ValueError) as error:
            raise CommandLogError("后台任务保存失败；执行结果可能未知，未自动重跑。") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 临时文件不参与恢复；清理失败不能覆盖原日志故障。
                    pass

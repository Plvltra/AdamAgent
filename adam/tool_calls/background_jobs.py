"""会话拥有的后台命令进程；后台线程只收集输出与终态，不访问会话存储。"""

import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from adam.contracts.errors import ToolError
from adam.tool_calls.command_output import OutputCapture
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.processes import start_command, terminate_process


@dataclass
class _Job:
    """只在管理器中保留活进程；输出捕获自身线程安全，终态在done之后可见。"""

    owner: str
    process: subprocess.Popen[bytes]
    stdout: OutputCapture
    stderr: OutputCapture
    started: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    duration: float | None = None
    worker: threading.Thread | None = None


class BackgroundJobs:
    """显式宿主API；确认在调用线程执行，任务不能跨owner查询或取消。

    当前只保留内存任务，调用者退出前必须close。启动与磁盘记录的协调由应用层负责；
    本模块不是持久服务，也不保证宿主被强杀后自动回收逃逸的子进程。
    """

    def __init__(self, commands: CommandTools, *, max_running: int = 2, max_jobs: int = 32) -> None:
        if not 1 <= max_running <= max_jobs <= 128:
            raise ValueError("invalid background job limits")
        self.commands = commands
        self.max_running, self.max_jobs = max_running, max_jobs
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        self._lock = threading.RLock()

    def start(self, owner: str, args: dict[str, Any]) -> dict[str, Any]:
        """校验并确认具体argv/cwd后启动，立即返回任务ID；拒绝不会创建进程。

        owner是宿主提供的会话身份，不从模型参数取得。调用线程串行批准和登记，
        最多保留max_jobs个任务，满后须新建管理器，避免无限保留终态。
        """
        if not owner:
            raise ToolError("后台任务缺少会话身份。")
        command = self.commands.prepare(args)
        with self._lock:
            if self._closed:
                raise ToolError("后台任务管理器已经关闭。")
            if (
                len(self._jobs) >= self.max_jobs
                or sum(not job.done.is_set() for job in self._jobs.values()) >= self.max_running
            ):
                raise ToolError("后台任务达到数量上限，请等待现有任务结束。")
            if self.commands.confirm is None or not self.commands.confirm(command):
                return {"ok": False, "status": "denied"}
            try:
                process = start_command(command)
            except OSError as error:
                raise ToolError("后台命令无法启动，未自动重试。") from error
            identifier = secrets.token_hex(8)
            job = _Job(
                owner,
                process,
                OutputCapture(self.commands.capture_bytes),
                OutputCapture(self.commands.capture_bytes),
                time.monotonic(),
            )
            self._jobs[identifier] = job
            job.worker = threading.Thread(
                target=self._run, args=(job, command.timeout_seconds), daemon=True
            )
            try:
                job.worker.start()
            except BaseException:
                terminate_process(process)
                del self._jobs[identifier]
                raise
            return {"ok": True, "task_id": identifier, "status": "running"}

    def snapshot(self, owner: str, identifier: str) -> dict[str, Any]:
        """读取有界输出和状态，不启动或重跑进程；非零退出码仍保持真实数值。"""
        job = self._owned(owner, identifier)
        done = job.done.is_set()
        out, err = job.stdout.snapshot(), job.stderr.snapshot()
        return {
            "ok": True,
            "task_id": identifier,
            "status": job.status if done else "running",
            "exit_code": job.process.returncode if done else None,
            "elapsed_seconds": job.duration if done else time.monotonic() - job.started,
            "stdout": out.text,
            "stderr": err.text,
            "output_capture": {"stdout": out.metadata(), "stderr": err.metadata()},
            "truncated": out.truncated or err.truncated,
        }

    def read_output(
        self,
        owner: str,
        identifier: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 4096,
    ) -> dict[str, Any]:
        """分别续读 stdout/stderr，不消费其他读者的位置；两流不承诺全局时间顺序。

        每条流最多返回 max_bytes 原始字节；调用方保存各自 next_cursor。
        非法或超前游标报 ToolError，查询不会重启进程或申请批准。
        """
        job = self._owned(owner, identifier)
        try:
            out = job.stdout.read_since(stdout_cursor, max_bytes)
            err = job.stderr.read_since(stderr_cursor, max_bytes)
        except ValueError as error:
            raise ToolError(str(error)) from error
        return {
            "ok": True,
            "task_id": identifier,
            "status": job.status if job.done.is_set() else "running",
            "stdout": out,
            "stderr": err,
        }

    def cancel(self, owner: str, identifier: str) -> None:
        """请求停止所属任务；实际回收完成前snapshot仍显示running，不伪造终态。"""
        self._owned(owner, identifier).cancel.set()

    def wait(self, owner: str, identifier: str, seconds: float = 1) -> dict[str, Any]:
        """最多等待10秒并返回当前观察；等待超时不会重新执行或取消命令。"""
        if not 0 <= seconds <= 10:
            raise ToolError("等待时间必须在0到10秒之间。")
        self._owned(owner, identifier).done.wait(seconds)
        return self.snapshot(owner, identifier)

    def close(self) -> None:
        """停止接收任务，取消并等待全部已启动进程回收；允许重复调用。"""
        with self._lock:
            self._closed = True
            jobs = tuple(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            if job.worker is not None:
                job.worker.join()

    def _owned(self, owner: str, identifier: str) -> _Job:
        with self._lock:
            job = self._jobs.get(identifier)
        if job is None or job.owner != owner:
            raise ToolError("后台任务不存在或不属于当前会话。")
        return job

    @staticmethod
    def _run(job: _Job, timeout: int) -> None:
        """排空两条管道并结算一次终态；超时、取消及异常均先回收直接子进程。"""
        assert job.process.stdout is not None and job.process.stderr is not None
        readers = [
            threading.Thread(target=job.stdout.drain, args=(job.process.stdout, True), daemon=True),
            threading.Thread(target=job.stderr.drain, args=(job.process.stderr, True), daemon=True),
        ]
        try:
            for reader in readers:
                reader.start()
            while job.process.poll() is None:
                if job.cancel.wait(0.02):
                    job.status = "cancelled"
                    terminate_process(job.process)
                    break
                if time.monotonic() - job.started >= timeout:
                    job.status = "timed_out"
                    terminate_process(job.process)
                    break
            else:
                job.status = "completed"
        except Exception:
            job.status = "failed"
            terminate_process(job.process)
        finally:
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=2)
            job.duration = round(time.monotonic() - job.started, 3)
            job.done.set()

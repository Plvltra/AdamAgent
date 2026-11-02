"""独立管道终端：保留进程的cwd/env和stdin，不模拟图形终端或TTY。"""

import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.models import CommandRequest
from adam.contracts.terminals import TerminalRequest
from adam.tool_calls.command_output import OutputCapture
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.processes import start_command, terminate_process


@dataclass
class _Terminal:
    """活进程只驻内存；worker回收后done置位，不能凭旧PID恢复执行。"""

    owner: str
    command: CommandRequest
    process: subprocess.Popen[bytes]
    stdout: OutputCapture
    stderr: OutputCapture
    started: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    writing: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None
    writer: threading.Thread | None = None
    status: str = "running"
    input_status: str = "idle"


class TerminalSessions:
    """宿主协调线程独占API；每会话/终端隔离，启动和每次输入分别批准。

    调用方退出前必须close。stdin写入在线程中完成，接受不表示子程序已消费。
    管道模式支持shell和行交互程序；依赖TTY的全屏程序不在本版范围。
    """

    def __init__(
        self, commands: CommandTools, confirm: Callable[[TerminalRequest], bool] | None = None
    ) -> None:
        self.commands, self.confirm = commands, confirm
        self._sessions: dict[str, _Terminal] = {}
        self._closed = False

    def start(
        self, owner: str, args: dict[str, Any], *, identifier: str | None = None
    ) -> dict[str, Any]:
        """批准完整argv/cwd/env后启动独立进程；最多2个活终端、32条运行记录。

        identifier仅供宿主在启动前保存意图时预分配；不能复用已有ID。
        """
        if self._closed or not owner:
            raise ToolError("终端管理器已关闭或缺少会话。")
        if (
            len(self._sessions) >= 32
            or sum(not t.done.is_set() for t in self._sessions.values()) >= 2
        ):
            raise ToolError("终端数量达到上限。")
        command = self.commands.prepare(args)
        env = args.get("env", {})
        if (
            not isinstance(env, dict)
            or len(env) > 32
            or any(
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\0" in k
                or not isinstance(v, str)
                or "\0" in v
                or len(k) + len(v) > 4096
                for k, v in env.items()
            )
        ):
            raise ToolError("env必须为有界字符串映射。")
        identity = identifier or secrets.token_hex(8)
        if (
            len(identity) != 16
            or any(c not in "0123456789abcdef" for c in identity)
            or identity in self._sessions
        ):
            raise ToolError("终端ID无效或已存在。")
        request = TerminalRequest("start", identity, command, tuple(env.items()))
        if not self.confirm or not self.confirm(request):
            return {"ok": False, "status": "denied"}
        try:
            process = start_command(command, interactive=True, environment=dict(request.env))
        except OSError as error:
            raise ToolError("终端无法启动，未自动重试。") from error
        terminal = _Terminal(
            owner,
            command,
            process,
            OutputCapture(self.commands.capture_bytes),
            OutputCapture(self.commands.capture_bytes),
        )
        terminal.worker = threading.Thread(target=self._run, args=(terminal,), daemon=True)
        try:
            terminal.worker.start()
        except BaseException:
            terminate_process(process)
            raise
        self._sessions[identity] = terminal
        return {"ok": True, "terminal_id": identity, "status": "running"}

    def send(self, owner: str, identity: str, text: str, *, eof: bool = False) -> dict[str, Any]:
        """批准原样UTF-8输入后异步写入，最多一个在途写入；eof表示写完关闭stdin。"""
        terminal = self._owned(owner, identity)
        if not isinstance(text, str) or len(text.encode("utf-8")) > 8192 or type(eof) is not bool:
            raise ToolError("输入须为最多8192字节文本，eof须为布尔值。")
        if terminal.done.is_set() or terminal.input_status in {"closed", "failed"}:
            raise ToolError("终端或输入通道已关闭。")
        if terminal.writing.is_set():
            raise ToolError("上次终端输入仍在写入，请稍后查询。")
        request = TerminalRequest("input", identity, terminal.command, text=text, eof=eof)
        if not self.confirm or not self.confirm(request):
            return {"ok": False, "status": "denied", "terminal_id": identity}
        terminal.writing.set()
        terminal.input_status = "pending"
        terminal.writer = threading.Thread(
            target=self._write, args=(terminal, text.encode("utf-8"), eof), daemon=True
        )
        try:
            terminal.writer.start()
        except BaseException:
            terminal.writing.clear()
            terminal.input_status = "failed"
            raise
        return {"ok": True, "terminal_id": identity, "input_status": "accepted"}

    def read(
        self, owner: str, identity: str, *, stdout_cursor: int = 0, stderr_cursor: int = 0
    ) -> dict[str, Any]:
        """使用B26字节游标读取有界双流；退出码属于终端进程，不推断单条命令结果。"""
        terminal = self._owned(owner, identity)
        try:
            out = terminal.stdout.read_since(stdout_cursor, 1024)
            err = terminal.stderr.read_since(stderr_cursor, 1024)
        except ValueError as error:
            raise ToolError(str(error)) from error
        return {
            "ok": True,
            "terminal_id": identity,
            "status": terminal.status if terminal.done.is_set() else "running",
            "exit_code": terminal.process.returncode if terminal.done.is_set() else None,
            "input_status": terminal.input_status,
            "stdout": out,
            "stderr": err,
        }

    def stop(self, owner: str, identity: str) -> None:
        """取消所属终端并等待进程回收；不影响其他终端。"""
        terminal = self._owned(owner, identity)
        terminal.cancel.set()
        if terminal.worker:
            terminal.worker.join()

    def close(self) -> None:
        """回收全部终端，可重复关闭；不支持宿主重启后重连旧进程。"""
        self._closed = True
        for terminal in self._sessions.values():
            terminal.cancel.set()
        for terminal in self._sessions.values():
            if terminal.worker:
                terminal.worker.join()

    def _owned(self, owner: str, identity: str) -> _Terminal:
        terminal = self._sessions.get(identity)
        if terminal is None or terminal.owner != owner:
            raise ToolError("终端不存在或不属于当前会话。")
        return terminal

    @staticmethod
    def _write(terminal: _Terminal, data: bytes, eof: bool) -> None:
        try:
            assert terminal.process.stdin is not None
            terminal.process.stdin.write(data)
            terminal.process.stdin.flush()
            if eof:
                terminal.process.stdin.close()
            terminal.input_status = "closed" if eof else "written"
        except (OSError, ValueError):
            terminal.input_status = "failed"
        finally:
            terminal.writing.clear()

    @staticmethod
    def _run(terminal: _Terminal) -> None:
        """复用有界双流捕获与进程树回收；超时约束整个终端寿命。"""
        assert terminal.process.stdout is not None and terminal.process.stderr is not None
        readers = [
            threading.Thread(target=capture.drain, args=(pipe, True), daemon=True)
            for capture, pipe in (
                (terminal.stdout, terminal.process.stdout),
                (terminal.stderr, terminal.process.stderr),
            )
        ]
        try:
            for reader in readers:
                reader.start()
            while terminal.process.poll() is None:
                if terminal.cancel.wait(0.02):
                    terminal.status = "cancelled"
                    terminate_process(terminal.process)
                    break
                if time.monotonic() - terminal.started >= terminal.command.timeout_seconds:
                    terminal.status = "timed_out"
                    terminate_process(terminal.process)
                    break
            else:
                terminal.status = "completed"
        except Exception:
            terminal.status = "failed"
            terminate_process(terminal.process)
        finally:
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=2)
            if terminal.writer and terminal.writer.ident is not None:
                terminal.writer.join(timeout=2)
            if terminal.process.stdin and not terminal.writing.is_set():
                try:
                    terminal.process.stdin.close()
                except OSError:
                    # 子进程退出后关闭管道可能再次报告BrokenPipe，不能阻止终态结算。
                    terminal.input_status = "failed"
            terminal.done.set()

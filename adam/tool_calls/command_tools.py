"""需逐次确认的通用命令执行；参数列表启动，不提供操作系统沙箱。"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from adam.errors import CommandLogError, ToolError
from adam.models import CommandRequest, ExecutionRecorder
from adam.settings import ToolLimits


class _Output:
    """持续排空管道，仅保留有界字节，防止大量输出阻塞子进程。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            with stream:
                while chunk := stream.read(4096):
                    remaining = max(0, self.limit - len(self.data))
                    self.data.extend(chunk[:remaining])
                    self.truncated |= len(chunk) > remaining
        except OSError:
            self.truncated = True


class CommandTools:
    def __init__(
        self,
        workspace: Path,
        limits: ToolLimits,
        confirm: Callable[[CommandRequest], bool] | None = None,
    ) -> None:
        self.workspace = workspace
        self.limits = limits
        self.confirm = confirm

    def run_command(
        self, args: dict[str, Any], record: ExecutionRecorder | None = None
    ) -> dict[str, Any]:
        """确认后执行 argv，返回退出码、输出、耗时及终止状态。

        cwd 相对启动工作目录解析；用户确认覆盖本次具体命令和目录。
        无确认回调默认拒绝；日志失败抛出 CommandLogError，禁止自动重试。
        """
        command = self._prepare(args)
        details = {
            "argv": list(command.argv),
            "cwd": str(command.cwd),
            "timeout_seconds": command.timeout_seconds,
        }
        approved = self.confirm(command) if self.confirm else False
        if not approved:
            result = self._result("denied")
            self._record(record, "command_denied", {**details, **result})
            return result

        # 先落盘开始事件，再启动进程；崩溃留下的开始事件不会被误判为成功。
        self._record(record, "command_started", details)
        started = time.monotonic()
        try:
            result = self._run(command)
        except (KeyboardInterrupt, EOFError):
            self._record(record, "command_finished", self._result("interrupted", started))
            raise
        self._record(record, "command_finished", result)
        return result

    def encode_result(self, output: dict[str, Any]) -> str:
        """裁剪命令输出时保留退出码和状态，避免模型误把截断预览当执行结果。"""
        encoded = json.dumps(output, ensure_ascii=False)
        while len(encoded) > self.limits.max_result_chars:
            key = "stdout" if len(output["stdout"]) >= len(output["stderr"]) else "stderr"
            output[key] = output[key][: len(output[key]) // 2]
            output["truncated"] = True
            encoded = json.dumps(output, ensure_ascii=False)
        return encoded

    def _prepare(self, args: dict[str, Any]) -> CommandRequest:
        """校验通用参数并解析执行路径；确认界面展示的参数即实际执行参数。"""
        argv = args.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0].strip()
        ):
            raise ToolError("argv 必须是非空字符串数组，程序名不能为空，参数不能含空字符。")
        directory = args.get("cwd", ".")
        if not isinstance(directory, str) or not directory.strip() or "\0" in directory:
            raise ToolError("cwd 必须是非空目录路径。")
        cwd = Path(directory).expanduser()
        cwd = (cwd if cwd.is_absolute() else self.workspace / cwd).resolve()
        if not cwd.is_dir():
            raise ToolError("cwd 不存在或不是目录。")
        timeout = args.get("timeout_seconds", self.limits.command_timeout_seconds)
        if type(timeout) is not int or not 1 <= timeout <= self.limits.command_timeout_seconds:
            raise ToolError(
                f"timeout_seconds 必须在 1 到 {self.limits.command_timeout_seconds} 之间。"
            )

        executable = Path(argv[0]).expanduser()
        if executable.is_absolute() or executable.parent != Path(".") or argv[0].startswith("."):
            executable = (executable if executable.is_absolute() else cwd / executable).resolve()
            program = str(executable)
        else:
            program = shutil.which(argv[0]) or argv[0]
        # Windows 批处理可能隐式经过 shell；需要时显式调用 shell 并展示完整脚本。
        if Path(program).suffix.lower() in {".bat", ".cmd"}:
            raise ToolError("批处理请显式通过 cmd.exe 调用，以便确认完整 shell 参数。")
        return CommandRequest((program, *argv[1:]), cwd, timeout)

    def _run(self, command: CommandRequest) -> dict[str, Any]:
        """捕获有界输出；超时或取消时终止进程树并回收直接子进程。"""
        started = time.monotonic()
        # 不继承 API 密钥等任意环境变量；保留 Windows/Python 启动需要的基础项。
        names = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "SYSTEMDRIVE",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in names}
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            return self._result("start_failed", started)

        assert process.stdout is not None and process.stderr is not None
        stdout = _Output(self.limits.max_result_chars)
        stderr = _Output(self.limits.max_result_chars)
        readers = [
            threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        status = "completed"
        try:
            process.wait(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            status = "timed_out"
            self._terminate(process)
        except BaseException:
            self._terminate(process)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=2)
        result = self._result(status, started)
        result.update(
            exit_code=process.returncode,
            stdout=stdout.data.decode("utf-8", errors="replace"),
            stderr=stderr.data.decode("utf-8", errors="replace"),
            truncated=stdout.truncated or stderr.truncated or any(r.is_alive() for r in readers),
        )
        return result

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """尽力终止进程树，保证直接子进程被回收；不是防逃逸的沙箱。"""
        try:
            if os.name == "nt":
                taskkill = (
                    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                )
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        process.wait()

    @staticmethod
    def _result(status: str, started: float | None = None) -> dict[str, Any]:
        return {
            "ok": status == "completed",
            "status": status,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": status == "timed_out",
            "truncated": False,
            "elapsed_seconds": round(time.monotonic() - started, 3) if started else 0.0,
        }

    @staticmethod
    def _record(record: ExecutionRecorder | None, kind: str, payload: dict[str, Any]) -> None:
        if record is not None:
            try:
                record(kind, payload)
            except OSError as error:
                raise CommandLogError(
                    "命令执行日志保存失败；执行状态可能未知，未自动重试。"
                ) from error

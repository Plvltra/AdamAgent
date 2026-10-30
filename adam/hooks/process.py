"""Hook 的有界标准输入输出进程；不复用工具审批，也不构成操作系统沙箱。"""

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from adam.contracts.cancellation import CancellationToken


@dataclass(frozen=True)
class ProcessResult:
    """保留直接进程退出码与有界原始输出；error_code 非空时不能解析为允许执行。"""

    stdout: bytes
    stderr: bytes
    exit_code: int | None
    error_code: str = ""


class _Pipe:
    """持续排空并记录超限；保留字节始终不超过上限。"""

    def __init__(self, limit: int, overflow: threading.Event, failure: threading.Event) -> None:
        self.limit, self.overflow, self.failure = limit, overflow, failure
        self.data = bytearray()

    def read(self, stream: BinaryIO) -> None:
        """超限或 IO 失败时通知协调线程，持续排空直至进程结束。"""
        try:
            with stream:
                while chunk := stream.read(4096):
                    available = max(0, self.limit - len(self.data))
                    self.data.extend(chunk[:available])
                    if len(chunk) > available:
                        self.overflow.set()
        except OSError:
            self.failure.set()


def run_process(
    argv: tuple[str, ...],
    workspace: Path,
    payload: bytes,
    timeout_seconds: float,
    output_limit: int,
    cancel: CancellationToken | None,
) -> ProcessResult:
    """单次启动检查器，超时/取消时尽力终止进程树并回收直接子进程。

    输入和两条输出并行传输，检查器不读 stdin 时主线程仍可取消或超时。
    不继承任意环境变量；响应上限分别应用于 stdout 和 stderr。
    """
    if cancel is not None:
        cancel.check()
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
    environment = {key: value for key, value in os.environ.items() if key.upper() in names}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    executable = Path(argv[0]).expanduser()
    if executable.is_absolute() or executable.parent != Path(".") or argv[0].startswith("."):
        program = str(
            (executable if executable.is_absolute() else workspace / executable).resolve()
        )
    else:
        program = shutil.which(argv[0]) or argv[0]
    if Path(program).suffix.lower() in {".bat", ".cmd"}:
        return ProcessResult(b"", b"", None, "start_failed")
    try:
        process = subprocess.Popen(
            (program, *argv[1:]),
            cwd=workspace,
            env=environment,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError:
        return ProcessResult(b"", b"", None, "start_failed")
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    overflow, failure = threading.Event(), threading.Event()
    stdout, stderr = _Pipe(output_limit, overflow, failure), _Pipe(output_limit, overflow, failure)

    def write_input() -> None:
        """独立写入后关闭 stdin，让读取到 EOF 的检查器能够完成。"""
        assert process.stdin is not None
        try:
            with process.stdin:
                process.stdin.write(payload)
                process.stdin.flush()
        except OSError:
            failure.set()

    workers = [
        threading.Thread(target=write_input, daemon=True),
        threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
    ]
    error_code = ""
    deadline = time.monotonic() + timeout_seconds
    try:
        for worker in workers:
            worker.start()
        while process.poll() is None or any(worker.is_alive() for worker in workers):
            if cancel is not None:
                cancel.check()
            if overflow.is_set():
                error_code = "output_limit"
            elif failure.is_set():
                error_code = "io_failed"
            elif time.monotonic() >= deadline:
                error_code = "timed_out"
            if error_code:
                _terminate(process)
                break
            time.sleep(0.01)
    except BaseException:
        _terminate(process)
        raise
    finally:
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=0.2)
    # 管道可能在进程退出的同一轮刚刚报告超限，不能把残缺响应判为通过。
    error_code = error_code or ("output_limit" if overflow.is_set() else "")
    error_code = error_code or ("io_failed" if failure.is_set() else "")
    return ProcessResult(bytes(stdout.data), bytes(stderr.data), process.returncode, error_code)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """尽力终止进程树；直接进程必须回收，不承诺阻止受信检查器主动逃逸。"""
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
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

"""工具进程终止与回收；供通用命令和固定 Git 查询共用。"""

import os
import signal
import subprocess
from pathlib import Path


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    """尽力终止进程树，保证直接子进程被回收；不是防逃逸的沙箱。"""
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

"""工具进程终止与回收；供通用命令和固定 Git 查询共用。"""

import os
import signal
import subprocess
from pathlib import Path

from adam.contracts.models import CommandRequest


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


def start_command(command: CommandRequest) -> subprocess.Popen[bytes]:
    """启动已批准的命令；固定环境白名单、无shell、关闭stdin，调用者负责回收。"""
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
    return subprocess.Popen(
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

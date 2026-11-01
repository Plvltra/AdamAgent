"""Windows 宿主到既定 WSL/Bubblewrap 的边界；所有执行先通过真实能力探测。"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from adam.contracts.errors import IsolationError

POLICY = "bubblewrap-single-process-v1"
CHECKS = frozenset(
    {
        "cpu_limit",
        "exec_denied",
        "execveat_denied",
        "fork_denied",
        "home_hidden",
        "host_mount_hidden",
        "memory_limit",
        "network_denied",
        "no_new_privileges",
        "root_read_only",
        "seccomp_filter",
        "uid_non_root",
    }
)
MAX_CODE_BYTES = 65536


@dataclass(frozen=True)
class IsolationCapability:
    """当次真实探测的结果；available 只涵盖本固定策略，不代表任意命令可运行。"""

    available: bool
    reason: str
    checks: tuple[str, ...] = ()
    policy: str = POLICY


@dataclass(frozen=True)
class IsolatedResult:
    """用户代码的退出事实；非零退出、输出超限和墙钟停止均不是成功。"""

    exit_code: int
    reason: str
    stdout: str
    stderr: str
    duration_ms: int
    policy: str = POLICY


class WslBubblewrap:
    """显式选择一个发行版，只提供无宿主挂载、无网络、无派生进程的 Python 执行。

    不安装依赖或修改 WSL 配置。发行版需有 /usr/bin/bwrap、Python 3.8+ 和
    libseccomp；固定限制为 256 MiB 地址空间、2 秒 CPU、5 秒墙钟及 64 KiB 双流。
    """

    def __init__(self, distribution: str = "Ubuntu") -> None:
        if not isinstance(distribution, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", distribution
        ):
            raise IsolationError("WSL 发行版名称无效。")
        self.distribution = distribution

    def probe(self) -> IsolationCapability:
        """实际运行固定探针；缺失、权限不足、超时或协议异常都返回不可用。"""
        try:
            value = self._invoke("probe", b"")
            checks = value.get("checks")
            if (
                value.get("available") is True
                and isinstance(checks, dict)
                and set(checks) == CHECKS
                and all(flag is True for flag in checks.values())
                and value.get("exit_code") == 0
                and value.get("reason") == "exited"
            ):
                return IsolationCapability(True, "verified", tuple(sorted(checks)))
            return IsolationCapability(False, "backend_unavailable")
        except IsolationError:
            return IsolationCapability(False, "backend_unavailable")

    def run_python(self, code: str) -> IsolatedResult:
        """探测成功后执行有界代码；不读取项目配置、API 密钥或真实会话。

        代码可使用完整 Python 语言，权限来自 OS 策略。普通运行错误保留 stdout、
        stderr 和退出码；隔离或通信失败抛 IsolationError，不声称代码未产生任何行为。
        """
        if not isinstance(code, str):
            raise IsolationError("隔离代码必须是文本。")
        try:
            raw = code.encode("utf-8")
        except UnicodeError as error:
            raise IsolationError("隔离代码必须是有效 UTF-8。") from error
        if not raw or len(raw) > MAX_CODE_BYTES:
            raise IsolationError("隔离代码须为 1～65536 字节。")
        if not self.probe().available:
            raise IsolationError("WSL/Bubblewrap 隔离不可用，未执行代码；不回退到宿主。")
        value = self._invoke("run", raw)
        if (
            set(value) != {"policy", "reason", "exit_code", "stdout", "stderr", "duration_ms"}
            or type(value["exit_code"]) is not int
            or type(value["duration_ms"]) is not int
            or not 0 <= value["duration_ms"] <= 30000
            or not isinstance(value["reason"], str)
            or value["reason"] not in {"exited", "wall_limit", "output_limit"}
            or not isinstance(value["stdout"], str)
            or not isinstance(value["stderr"], str)
            or len(value["stdout"]) + len(value["stderr"]) > 65536
        ):
            raise IsolationError("隔离执行返回格式不完整，结果未知；未自动重跑。")
        return IsolatedResult(
            value["exit_code"],
            value["reason"],
            value["stdout"],
            value["stderr"],
            value["duration_ms"],
        )

    def _invoke(self, operation: str, payload: bytes) -> dict[str, object]:
        """固定 argv 和启动器路径；不经 shell，子环境不传入 API、WSLENV 等变量。"""
        if os.name != "nt":
            raise IsolationError("当前后端只支持 Windows 上显式选择的 WSL 发行版。")
        path = PureWindowsPath(Path(__file__).with_name("linux_worker.py").resolve())
        if not re.fullmatch(r"[A-Za-z]:", path.drive):
            raise IsolationError("隔离启动器需要位于本地盘符路径。")
        # 首版仅支持默认 /mnt/<盘符> 映射；不同挂载布局明确报不可用，不猜另一份代码。
        worker = "/mnt/" + path.drive[0].lower() + "/" + "/".join(path.parts[1:])
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        executable = str(Path(system_root) / "System32" / "wsl.exe")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "--distribution",
                    self.distribution,
                    "--exec",
                    "/usr/bin/python3",
                    "-I",
                    worker,
                    operation,
                ],
                input=payload,
                capture_output=True,
                timeout=30,
                env={"SystemRoot": system_root, "PATH": str(Path(system_root) / "System32")},
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if completed.returncode != 0 or len(completed.stdout) > 524288:
                raise IsolationError("隔离后端启动或通信失败，未自动重试。")
            value = json.loads(completed.stdout)
            if not isinstance(value, dict) or value.get("policy") != POLICY:
                raise IsolationError("隔离后端策略版本不匹配。")
            return value
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise IsolationError("隔离后端不可用或通信中断，未自动重试。") from error

"""显式 WSL 原生命令入口；与 B14 计算策略分开，失败从不改走宿主命令。"""

import asyncio
import json
import os
from pathlib import Path, PureWindowsPath

from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_execution import CommandIsolationResult as CommandIsolationResult
from adam.contracts.command_inputs import CommandInput
from adam.contracts.errors import IsolationError
from adam.isolation.backend import WslBubblewrap
from adam.isolation.command_protocol import MAX_RESPONSE_BYTES, POLICY, request

PROBE = """
import ctypes, errno, json, os, pathlib, resource, socket
checks = {'workspace_empty': list(pathlib.Path('/work').iterdir()) == [],
          'host_hidden': not pathlib.Path('/mnt').exists(),
          'memory_limit': resource.getrlimit(resource.RLIMIT_AS) == (268435456, 268435456),
          'cpu_limit': resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)}
for name, action in [('file_write_denied', lambda: open('/work/probe', 'w')),
                     ('network_denied', lambda: socket.socket()),
                     ('other_exec_denied', lambda: os.execv('/bin/true', ['true'])),
                     ('fork_denied', os.fork)]:
    try:
        value = action()
    except OSError as error:
        checks[name] = error.errno in (errno.EROFS, errno.EPERM, errno.EACCES)
    else:
        if name == 'fork_denied' and value == 0:
            os._exit(99)
        checks[name] = False
lib = ctypes.CDLL(None, use_errno=True)
# x86_64 memfd_create；旧发行版 Python 不一定提供 os.memfd_create。
checks['memfd_denied'] = lib.syscall(319, b'adam', 0) == -1 and ctypes.get_errno() == errno.EPERM
print(json.dumps(checks))
"""
CHECKS = frozenset(
    {
        "workspace_empty",
        "host_hidden",
        "memory_limit",
        "cpu_limit",
        "file_write_denied",
        "network_denied",
        "other_exec_denied",
        "fork_denied",
        "memfd_denied",
    }
)


class IsolatedCommands:
    """原生单进程命令：只读输入快照、无网络、固定资源上限；不直接读取宿主项目。"""

    def __init__(self, distribution: str = "Ubuntu") -> None:
        self.backend = WslBubblewrap(distribution)

    def probe(self, *, cancel: CancellationToken | None = None) -> bool:
        """实际运行完整固定探针；一个能力缺失就拒绝，不缓存前一次机器状态。"""
        try:
            result = self._invoke(
                request(["python3", "-I", "-B", "-c", PROBE], 5), cancel or CancellationToken()
            )
            checks = json.loads(result.stdout)
            return (
                result.reason == "exited"
                and result.exit_code == 0
                and isinstance(checks, dict)
                and set(checks) == CHECKS
                and all(flag is True for flag in checks.values())
            )
        except (IsolationError, ValueError):
            return False

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        timeout_seconds: int = 5,
        inputs: tuple[CommandInput, ...] = (),
        cancel: CancellationToken | None = None,
    ) -> CommandIsolationResult:
        """探测后运行原生 argv；失败抛 IsolationError，取消会关闭通道并回收启动器。

        inputs 只接收已授权的有界字节快照，在 /work 中只读呈现。不传宿主来源路径，
        不安装程序，不执行 Windows 程序，不挂载项目目录，也不自动重试命令。
        """
        token = cancel or CancellationToken()
        token.check()
        try:
            payload = request(argv, timeout_seconds, inputs)
        except (ValueError, UnicodeError) as error:
            raise IsolationError(
                "隔离命令参数或输入无效；argv 最多128项/64KiB，输入最多16个/2MiB，期限1～5秒。"
            ) from error
        if not self.probe(cancel=token):
            raise IsolationError("原生命令隔离不可用，未运行命令；不回退宿主。")
        token.check()
        return self._invoke(payload, token)

    def _invoke(self, payload: bytes, token: CancellationToken) -> CommandIsolationResult:
        """固定 Windows/WSL 启动入口，协议异常不返回伪造的成功结果。"""
        if os.name != "nt":
            raise IsolationError("原生命令后端需要 Windows WSL。")
        try:
            return asyncio.run(self._exchange(payload, token))
        except (OSError, ValueError, TimeoutError) as error:
            raise IsolationError("隔离命令通信失败，执行结果可能未知；未自动重跑。") from error

    async def _exchange(self, payload: bytes, token: CancellationToken) -> CommandIsolationResult:
        """维持宿主管道直到收到唯一完整结果；取消时关闭输入，让 Linux 端清理。"""
        root = PureWindowsPath(Path(__file__).resolve().parents[2])
        if len(root.drive) != 2 or root.drive[1] != ":":
            raise IsolationError("隔离启动器需要本地盘符路径。")
        linux_root = "/mnt/" + root.drive[0].lower() + "/" + "/".join(root.parts[1:])
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        launcher = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from adam.isolation.linux_command import main; sys.exit(main())"
        )
        process = await asyncio.create_subprocess_exec(
            str(Path(system_root) / "System32/wsl.exe"),
            "--distribution",
            self.backend.distribution,
            "--exec",
            "/usr/bin/python3",
            "-I",
            "-c",
            launcher,
            linux_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"SystemRoot": system_root, "PATH": str(Path(system_root) / "System32")},
            limit=MAX_RESPONSE_BYTES,
            creationflags=0x08000000,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(payload)
            await asyncio.wait_for(process.stdin.drain(), timeout=5)
            deadline = asyncio.get_running_loop().time() + 15
            while True:
                token.check()
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("isolated command deadline")
                try:
                    raw = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                except TimeoutError:
                    continue
                break
            if not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("invalid isolated result frame")
            process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode != 0 or await process.stdout.read(1):
                raise ValueError("invalid isolated launcher exit")
            return _result(json.loads(raw))
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()


def _result(value: object) -> CommandIsolationResult:
    """按固定策略校验结果；命令的 stdout 不作为可信控制帧解析。"""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"policy", "exit_code", "reason", "stdout", "stderr", "captured_bytes", "duration_ms"}
        or value["policy"] != POLICY
        or type(value["exit_code"]) is not int
        or value["reason"] not in ("exited", "wall_limit", "output_limit")
        or not isinstance(value["stdout"], str)
        or not isinstance(value["stderr"], str)
        or len(value["stdout"]) + len(value["stderr"]) > 65536
        or type(value["captured_bytes"]) is not int
        or not 0 <= value["captured_bytes"] <= 65536
        or len(value["stdout"]) + len(value["stderr"]) > value["captured_bytes"]
        or type(value["duration_ms"]) is not int
        or not 0 <= value["duration_ms"] <= 15000
    ):
        raise ValueError("invalid isolated command result")
    return CommandIsolationResult(
        value["exit_code"],
        value["reason"],
        value["stdout"],
        value["stderr"],
        value["captured_bytes"],
        value["duration_ms"],
    )

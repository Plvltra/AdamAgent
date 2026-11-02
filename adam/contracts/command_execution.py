"""隔离命令的执行端口和批准资料；工具层无需了解 WSL 的进程实现。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_inputs import CommandInput


@dataclass(frozen=True)
class CommandInputPreview:
    """本次批准的虚拟名、宿主源路径和实际字节版本；不包含原文件内容。"""

    name: str
    source: str
    version: str


@dataclass(frozen=True)
class CommandIsolationResult:
    """真实退出事实；非零、停止原因及截断分别保留，不推断业务成功。"""

    exit_code: int
    reason: str
    stdout: str
    stderr: str
    captured_bytes: int
    duration_ms: int
    policy: str = "bubblewrap-native-single-process-v1"


class CommandRunner(Protocol):
    """宿主注入固定隔离后端；输入是已授权快照，失败绝不回退到宿主执行。"""

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        timeout_seconds: int = 5,
        inputs: tuple[CommandInput, ...] = (),
        cancel: CancellationToken | None = None,
    ) -> CommandIsolationResult:
        """最多执行一次命令；取消和通信错误向上传播，不能伪造退出码或自动重跑。"""
        ...

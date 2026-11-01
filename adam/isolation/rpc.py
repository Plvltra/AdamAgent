"""在协调线程处理隔离脚本请求；只有宿主显式传入的回调可以产生工具动作。"""

import asyncio
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.orchestration import OrchestrationResult, ToolCallback
from adam.isolation.backend import IsolationError, WslBubblewrap
from adam.isolation.rpc_protocol import (
    MAX_FRAME_BYTES,
    RpcProtocolError,
    decode_frame,
    encode_frame,
    validate_call,
)


class IsolatedOrchestrator:
    """固定 WSL 策略下的窄 RPC，不持有工具目录、权限或会话预算的实现。"""

    def __init__(self, backend: WslBubblewrap) -> None:
        self.backend = backend

    def run(
        self, code: str, invoke: ToolCallback, *, cancel: CancellationToken | None = None
    ) -> OrchestrationResult:
        """在当前协调线程转交调用；回调异常、中断或协议故障都停止，不重新派发。

        invoke 必须由宿主实现注册工具检查和原有执行流程；脚本不能指定这个回调。
        本方法会驱动自己的事件循环，不能在已运行的 asyncio 循环中同步调用。
        """
        token = cancel or CancellationToken()
        token.check()
        if not isinstance(code, str):
            raise IsolationError("编排代码必须是文本。")
        try:
            if not 1 <= len(code.encode("utf-8")) <= 65536:
                raise IsolationError("编排代码须为 1～65536 字节。")
            initial = encode_frame({"kind": "start", "code": code}, limit=524288)
        except (UnicodeError, RpcProtocolError) as error:
            raise IsolationError("编排代码无法编码。") from error
        if not self.backend.probe().available:
            raise IsolationError("编排所需隔离不可用，未执行代码。")
        token.check()
        try:
            return asyncio.run(self._run(initial, invoke, token))
        except OSError as error:
            # 探测后启动器仍可能被删除或撤权；启动竞争也必须是可记录的停止原因。
            raise IsolationError("编排启动失败；未回退到宿主执行。") from error

    async def _run(
        self, initial: bytes, invoke: ToolCallback, token: CancellationToken
    ) -> OrchestrationResult:
        """使用异步管道等待，但业务回调不移到其他线程；退出时关闭输入并回收启动器。"""
        root = PureWindowsPath(Path(__file__).resolve().parents[2])
        if len(root.drive) != 2 or root.drive[1] != ":":
            raise IsolationError("编排启动器需要本地盘符路径。")
        linux_root = "/mnt/" + root.drive[0].lower() + "/" + "/".join(root.parts[1:])
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        launcher = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from adam.isolation.linux_rpc import main; sys.exit(main())"
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
            stderr=asyncio.subprocess.PIPE,
            env={"SystemRoot": system_root, "PATH": str(Path(system_root) / "System32")},
            limit=MAX_FRAME_BYTES,
            creationflags=0x08000000,
        )
        assert process.stdin is not None and process.stdout is not None
        calls = 0
        deadline = asyncio.get_running_loop().time() + 330
        try:
            process.stdin.write(initial)
            await asyncio.wait_for(process.stdin.drain(), timeout=5)
            while True:
                token.check()
                if asyncio.get_running_loop().time() > deadline:
                    raise IsolationError("编排宿主等待超过上限；已停止且未自动重跑。")
                try:
                    raw = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                except TimeoutError:
                    continue
                if not raw:
                    raise IsolationError("编排通道中断，结果未知；未自动重跑。")
                frame = decode_frame(raw)
                if frame.get("kind") == "call":
                    name, arguments = validate_call(frame, calls + 1)
                    calls += 1
                    token.check()
                    # 工具日志、批准和额度回调在原协调线程执行；异常不能降为脚本可忽略的值。
                    output = invoke(name, arguments)
                    token.check()
                    reply = encode_frame({"kind": "reply", "sequence": calls, "value": output})
                    process.stdin.write(reply)
                    await asyncio.wait_for(process.stdin.drain(), timeout=5)
                elif frame.get("kind") == "finished":
                    result = _finished(frame, calls)
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), timeout=5)
                    if process.returncode != 0 or await process.stdout.read(1):
                        raise RpcProtocolError("编排退出状态或尾部消息无效。")
                    return result
                else:
                    raise RpcProtocolError("编排启动器报告失败或未知消息。")
        except (RpcProtocolError, OSError, ValueError, TimeoutError) as error:
            raise IsolationError("编排协议或进程失败；已停止且未自动重跑。") from error
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()


def _finished(frame: dict[str, Any], calls: int) -> OrchestrationResult:
    """结束帧必须与宿主实际派发次数一致；不接受 worker 自报成功来覆盖退出事实。"""
    if (
        set(frame) != {"kind", "exit_code", "reason", "result", "has_result", "calls", "stderr"}
        or type(frame["exit_code"]) is not int
        or not isinstance(frame["reason"], str)
        or frame["reason"] not in {"exited", "wall_limit"}
        or type(frame["has_result"]) is not bool
        or type(frame["calls"]) is not int
        or frame["calls"] != calls
        or not isinstance(frame["stderr"], str)
        or len(frame["stderr"]) > 8000
        or (frame["has_result"] and (frame["exit_code"] != 0 or frame["reason"] != "exited"))
        or (not frame["has_result"] and frame["result"] is not None)
    ):
        raise RpcProtocolError("编排结束事实无效。")
    return OrchestrationResult(
        frame["result"],
        frame["has_result"],
        frame["exit_code"],
        frame["reason"],
        calls,
        frame["stderr"],
    )

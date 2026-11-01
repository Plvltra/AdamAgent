"""Linux 可信 RPC 转发器；用户代码无宿主句柄，所有工具动作仍由 Windows 宿主处理。"""

from __future__ import annotations

import base64
import os
import selectors
import subprocess
import sys
import tempfile
import time
from typing import Any

from adam.isolation.linux_worker import BOOTSTRAP_GUARD, command, kill_group, seccomp_filter
from adam.isolation.rpc_protocol import (
    MAX_FRAME_BYTES,
    MAX_RESULT_BYTES,
    RpcProtocolError,
    decode_frame,
    encode_frame,
    validate_call,
)

# stdout 只留给控制帧，普通 print 转向有界 stderr。它不是信任边界：宿主仍校验所有帧。
RPC_CLIENT = """
import base64, json, sys
wire = sys.stdout
sys.stdout = sys.stderr
sequence = 0
def call_tool(name, arguments):
    global sequence
    sequence += 1
    wire.write(json.dumps({'kind': 'call', 'sequence': sequence, 'name': name,
                           'arguments': arguments}, ensure_ascii=False, allow_nan=False) + '\\n')
    wire.flush()
    raw = sys.stdin.buffer.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b'\\n'):
        raise RuntimeError('tool_response_invalid')
    reply = json.loads(raw)
    if reply.get('kind') != 'reply' or reply.get('sequence') != sequence:
        raise RuntimeError('tool_response_mismatched')
    return reply['value']
namespace = {'__name__': '__main__', 'call_tool': call_tool}
exec(compile(base64.b64decode(sys.argv[1]), '<orchestration>', 'exec'), namespace)
wire.write(json.dumps({'kind': 'result', 'value': namespace.get('result')},
                      ensure_ascii=False, allow_nan=False) + '\\n')
wire.flush()
"""


def send(value: dict[str, Any]) -> None:
    """可信启动器向宿主发出一条完整帧；管道关闭就是停止，不自动重连。"""
    sys.stdout.buffer.write(encode_frame(value))
    sys.stdout.buffer.flush()


def supervise(code: bytes) -> None:
    """转发一问一答，计算阶段最多 5 秒，等待工具各最多 60 秒，总墙钟最多 300 秒。

    等待期间 CPU/内存/进程限制仍有效。任何损坏、额外请求或断线都终止进程；
    不根据 worker 的声明修改调用额度，也不在此执行工具。
    """
    with tempfile.TemporaryFile() as policy:
        seccomp_filter(policy.fileno())
        process = subprocess.Popen(
            command(policy.fileno(), bootstrap=BOOTSTRAP_GUARD + RPC_CLIENT)
            + [base64.b64encode(code).decode("ascii")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"},
            pass_fds=(policy.fileno(),),
            start_new_session=True,
        )
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        # worker 可以申请工具后拒绝读取回复；写管道不能阻塞外部的超时看守。
        if os.name == "nt":
            raise RuntimeError("linux_required")
        else:
            os.set_blocking(process.stdin.fileno(), False)
        started = active_since = time.monotonic()
        active_seconds = 0.0
        pending_since: float | None = None
        sequence = 0
        result: dict[str, Any] | None = None
        stderr = bytearray()
        buffers = {"child": bytearray(), "host": bytearray()}
        outgoing = bytearray()
        reason = "exited"
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "child")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                selector.register(sys.stdin.buffer, selectors.EVENT_READ, "host")
                while process.poll() is None or any(
                    key.data != "host" for key in selector.get_map().values()
                ):
                    now = time.monotonic()
                    if (
                        now - started > 300
                        or (pending_since is not None and now - pending_since > 60)
                        or (pending_since is None and active_seconds + now - active_since > 5)
                    ):
                        reason = "wall_limit"
                        break
                    for key, _ in selector.select(0.05):
                        if key.data == "reply":
                            try:
                                written = os.write(key.fd, outgoing)
                            except BlockingIOError:
                                continue
                            del outgoing[:written]
                            if not outgoing:
                                selector.unregister(key.fileobj)
                                pending_since = None
                                active_since = time.monotonic()
                            continue
                        block = os.read(key.fd, 8192)
                        if not block:
                            if key.data == "host":
                                raise RpcProtocolError("宿主已断开。")
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stderr":
                            if len(stderr) + len(block) > 65536:
                                raise RpcProtocolError("编排输出超过限制。")
                            stderr.extend(block)
                            continue
                        buffer = buffers[key.data]
                        buffer.extend(block)
                        if len(buffer) > MAX_FRAME_BYTES:
                            raise RpcProtocolError("编排帧超过限制。")
                        while b"\n" in buffer:
                            line, _, remainder = buffer.partition(b"\n")
                            buffer[:] = remainder
                            frame = decode_frame(bytes(line) + b"\n")
                            if key.data == "host":
                                if (
                                    pending_since is None
                                    or outgoing
                                    or set(frame) != {"kind", "sequence", "value"}
                                    or frame["kind"] != "reply"
                                    or type(frame["sequence"]) is not int
                                    or frame["sequence"] != sequence
                                ):
                                    raise RpcProtocolError("宿主回复顺序无效。")
                                outgoing.extend(encode_frame(frame))
                                selector.register(process.stdin, selectors.EVENT_WRITE, "reply")
                            elif frame.get("kind") == "call":
                                if pending_since is not None or result is not None:
                                    raise RpcProtocolError("同一时刻只允许一个工具请求。")
                                validate_call(frame, sequence + 1)
                                sequence += 1
                                pending_since = time.monotonic()
                                active_seconds += pending_since - active_since
                                send(frame)
                            elif set(frame) == {"kind", "value"} and frame["kind"] == "result":
                                if pending_since is not None or result is not None:
                                    raise RpcProtocolError("结果早于工具回复或重复。")
                                encode_frame(frame, limit=MAX_RESULT_BYTES)
                                result = frame
                            else:
                                raise RpcProtocolError("未知编排帧。")
                if reason != "exited":
                    kill_group(process)
                process.wait(timeout=1)
                if any(buffers.values()) or pending_since is not None:
                    raise RpcProtocolError("编排结束时仍有残缺消息。")
            send(
                {
                    "kind": "finished",
                    "exit_code": process.returncode,
                    "reason": reason,
                    "result": result["value"]
                    if result is not None and process.returncode == 0
                    else None,
                    "has_result": result is not None and process.returncode == 0,
                    "calls": sequence,
                    "stderr": stderr.decode("utf-8", errors="replace")[:8000],
                }
            )
        finally:
            if process.poll() is None:
                kill_group(process)
                process.wait(timeout=1)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


def main() -> int:
    """初始代码只读一次，不接受宿主路径、挂载参数或 shell 参数。"""
    try:
        initial = decode_frame(sys.stdin.buffer.readline(524289), limit=524288)
        if (
            set(initial) != {"kind", "code"}
            or initial["kind"] != "start"
            or not isinstance(initial["code"], str)
        ):
            raise RpcProtocolError("编排初始化无效。")
        code = initial["code"].encode("utf-8")
        if not 1 <= len(code) <= 65536:
            raise RpcProtocolError("编排代码大小无效。")
        supervise(code)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        send({"kind": "error", "reason": "rpc_failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Linux 可信命令看守；固定隔离、输入断开及墙钟/输出超限都在用户命令之外处理。"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from adam.contracts.command_inputs import CommandInput, validate_inputs
from adam.isolation.command_protocol import (
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    POLICY,
    read_request,
)
from adam.isolation.linux_worker import command, kill_group, seccomp_filter


def supervise(
    argv: list[str],
    timeout: int,
    inputs: tuple[CommandInput, ...] = (),
) -> dict[str, Any]:
    """只读 /work 快照中运行原生 argv；宿主断线会杀死进程，不接受进一步输入。

    返回最多 64 KiB 已捕获双流。超限只表示已读到该边界，不声称取得程序最终尾部。
    """
    bootstrap = Path(__file__).with_name("linux_command_guard.py").read_text(encoding="utf-8")
    started = time.monotonic()
    validate_inputs(inputs)
    with tempfile.TemporaryDirectory(prefix="adam-command-input-") as directory:
        for item in inputs:
            path = Path(directory).joinpath(*item.name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.data)
        return _execute(argv, timeout, bootstrap, started, directory)


def _execute(
    argv: list[str],
    timeout: int,
    bootstrap: str,
    started: float,
    directory: str,
) -> dict[str, Any]:
    """临时快照只读绑定；先杀死并回收用户进程，外层才删除自己的临时目录。"""
    with tempfile.TemporaryFile() as policy:
        seccomp_filter(policy.fileno(), extra_denied=("memfd_create",))
        process = subprocess.Popen(
            command(policy.fileno(), bootstrap=bootstrap, input_directory=directory)
            + [json.dumps(argv, ensure_ascii=True)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"},
            pass_fds=(policy.fileno(),),
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        reason, size = "exited", 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ, "host")
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while process.poll() is None or any(
                    key.data != "host" for key in selector.get_map().values()
                ):
                    if time.monotonic() - started >= timeout:
                        reason = "wall_limit"
                        break
                    for key, _ in selector.select(0.05):
                        block = os.read(key.fd, 8192)
                        if key.data == "host":
                            # 初始请求后唯一合法动作是保持通道；断开或额外消息都停止。
                            reason = "host_disconnected" if not block else "protocol_error"
                            break
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = MAX_OUTPUT_BYTES - size
                        buffers[key.data].extend(block[:remaining])
                        size += min(remaining, len(block))
                        if len(block) > remaining:
                            reason = "output_limit"
                            break
                    if reason != "exited":
                        break
            if reason != "exited":
                kill_group(process)
            process.wait(timeout=1)
        finally:
            if process.poll() is None:
                kill_group(process)
                process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()
        stdout, out_loss = _decode(buffers["stdout"])
        stderr, err_loss = _decode(buffers["stderr"])
        return {
            "policy": POLICY,
            "exit_code": process.returncode,
            "reason": reason,
            "stdout": stdout,
            "stderr": stderr,
            "invalid_utf8": out_loss or err_loss,
            "captured_bytes": size,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def _decode(data: bytearray) -> tuple[str, bool]:
    """替换不可解码片段时明确标记损失；合法的 U+FFFD 字符本身不算解码失败。"""
    try:
        return data.decode("utf-8"), False
    except UnicodeError:
        return data.decode("utf-8", errors="replace"), True


def main() -> int:
    """初始协议失败不启动进程；执行错误只回传固定类别，不回显宿主环境。"""
    try:
        argv, timeout, inputs = read_request(sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1))
        result = supervise(argv, timeout, inputs)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"policy": POLICY, "reason": "backend_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

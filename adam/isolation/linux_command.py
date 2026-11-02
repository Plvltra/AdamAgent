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

from adam.isolation.command_protocol import (
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    POLICY,
    read_request,
)
from adam.isolation.linux_worker import command, kill_group, seccomp_filter


def supervise(argv: list[str], timeout: int) -> dict[str, Any]:
    """只读空 /work 内运行原生 argv；宿主断线会杀死进程，不接受进一步输入。

    返回最多 64 KiB 已捕获双流。超限只表示已读到该边界，不声称取得程序最终尾部。
    """
    bootstrap = Path(__file__).with_name("linux_command_guard.py").read_text(encoding="utf-8")
    started = time.monotonic()
    with tempfile.TemporaryFile() as policy:
        seccomp_filter(policy.fileno(), extra_denied=("memfd_create",))
        process = subprocess.Popen(
            command(policy.fileno(), bootstrap=bootstrap) + [json.dumps(argv, ensure_ascii=True)],
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
        return {
            "policy": POLICY,
            "exit_code": process.returncode,
            "reason": reason,
            "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
            "captured_bytes": size,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def main() -> int:
    """初始协议失败不启动进程；执行错误只回传固定类别，不回显宿主环境。"""
    try:
        argv, timeout = read_request(sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1))
        result = supervise(argv, timeout)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"policy": POLICY, "reason": "backend_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

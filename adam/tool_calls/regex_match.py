"""可取消、限时的固定正则工作进程；不在Agent协调线程运行正则引擎。"""

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.tool_calls.regex_worker import MAX_INPUT_BYTES, MAX_PATTERN_CHARS, MAX_TEXT_BYTES


def match_lines(
    pattern: str,
    text: str,
    *,
    timeout_seconds: float = 1.0,
    cancel: CancellationToken | None = None,
) -> tuple[int, ...]:
    """返回从1开始的匹配行号；按行匹配，去掉行结束符，保留Python正则语义。

    输入最多256KiB UTF-8、模式最多1024字符；每个调用独立启动可信固定程序，
    超时或取消先杀死并回收直接子进程，再传播错误，不返回部分结果。
    限制输入、输出和等待时长，不宣称提供OS内存硬上限或通用代码沙箱。
    """
    if cancel:
        cancel.check()
    encoded = _request(pattern, text, timeout_seconds)
    worker = Path(__file__).with_name("regex_worker.py")
    # 临时文件代替管道，输入/输出有界且避免stdin写入阻塞阻碍取消。
    with tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as response:
        request.write(encoded)
        request.seek(0)
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(worker)],
            stdin=request,
            stdout=response,
            stderr=subprocess.DEVNULL,
            cwd=worker.parent,
            env={
                name: os.environ[name]
                for name in ("SystemRoot", "TEMP", "TMP")
                if name in os.environ
            },
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            _wait(process, timeout_seconds, cancel)
            response.seek(0)
            output = response.read(MAX_TEXT_BYTES + 5)
        except BaseException:
            # 固定脚本不会创建后代，无需为用户程序推断进程树或增加命令审批。
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            raise
    if cancel:
        cancel.check()
    if process.returncode != 0:
        raise ToolError("正则表达式无效或匹配资源不足，未返回部分结果。")
    count = sum(1 for _ in StringIO(text, newline=None))
    if not output.startswith(b"R\nO") or len(output) != count + 3 or set(output[3:]) - {48, 49}:
        raise ToolError("正则工作进程返回格式无效。")
    return tuple(index for index, value in enumerate(output[3:], 1) if value == 49)


def _request(pattern: str, text: str, timeout_seconds: float) -> bytes:
    """在启动前拒绝不合范围的输入；不允许关闭期限或通过编码扩大请求。"""
    if (
        not isinstance(pattern, str)
        or not 1 <= len(pattern) <= MAX_PATTERN_CHARS
        or not isinstance(text, str)
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        raise ToolError("正则模式或期限参数超限。")
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ToolError("正则输入超过256KiB，请缩小文件或改用字面搜索。")
        value = json.dumps({"pattern": pattern, "text": text}, ensure_ascii=False).encode("utf-8")
    except UnicodeError as error:
        raise ToolError("正则输入不是有效UTF-8。") from error
    if len(value) > MAX_INPUT_BYTES:
        raise ToolError("正则请求编码后超过大小限制。")
    return value


def _wait(
    process: subprocess.Popen[bytes], timeout_seconds: float, cancel: CancellationToken | None
) -> None:
    """等待期间检查取消和墙钟；只有进程退出才读取完整结果。"""
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        if cancel:
            cancel.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError("正则匹配超时，请简化表达式或缩小文本。")
        try:
            process.wait(timeout=min(0.02, remaining))
        except subprocess.TimeoutExpired:
            continue

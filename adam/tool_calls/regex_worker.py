"""固定正则工作进程：仅处理stdin中的文本，不导入项目、打开路径或执行模型代码。"""

import json
import re
import sys
from io import StringIO

# 由宿主使用 -I -S 和绝对脚本路径启动，因此这里仅依赖标准库。
MAX_TEXT_BYTES = 262144
MAX_PATTERN_CHARS = 1024
MAX_INPUT_BYTES = MAX_TEXT_BYTES * 6 + 16384


def main() -> int:
    """输出就绪标记及有界逐行位图；错误仅返回固定原因，不回显输入。"""
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        sys.stdout.buffer.write(b"Einput_limit")
        return 1
    try:
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"pattern", "text"}:
            raise ValueError("shape")
        pattern, text = request["pattern"], request["text"]
        if (
            not isinstance(pattern, str)
            or not 1 <= len(pattern) <= MAX_PATTERN_CHARS
            or not isinstance(text, str)
            or len(text.encode("utf-8")) > MAX_TEXT_BYTES
        ):
            raise ValueError("bounds")
    except (ValueError, UnicodeError):
        sys.stdout.buffer.write(b"Einvalid_input")
        return 1
    # 宿主可辨认请求已经读入；匹配期间不逐项输出，避免部分结果被误当作成功。
    sys.stdout.buffer.write(b"R\n")
    sys.stdout.buffer.flush()
    try:
        expression = re.compile(pattern)
        matched = bytearray()
        for line in StringIO(text, newline=None):
            matched.append(49 if expression.search(line.rstrip("\r\n")) else 48)
    except (re.error, OverflowError, RecursionError, MemoryError):
        sys.stdout.buffer.write(b"Einvalid_pattern_or_resources")
        return 1
    sys.stdout.buffer.write(b"O" + matched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""原生命令隔离的有界协议；宿主与 Linux 启动器共用，兼容 Python 3.8。"""

from __future__ import annotations

import json

POLICY = "bubblewrap-native-single-process-v1"
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 524288
MAX_OUTPUT_BYTES = 65536


def validate_argv(value: object) -> list[str]:
    """只接受有界参数列表；首版没有 shell 拼接、宿主路径或挂载参数。"""
    if (
        not isinstance(value, (tuple, list))
        or not 1 <= len(value) <= 128
        or any(not isinstance(part, str) or "\0" in part for part in value)
        or not value[0].strip()
        or any(len(part.encode("utf-8")) > 32768 for part in value)
    ):
        raise ValueError("invalid_command_argv")
    result = list(value)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES - 256:
        raise ValueError("command_argv_too_large")
    return result


def request(argv: object, timeout: object) -> bytes:
    """编码启动请求；墙钟上限只可调低，不允许覆盖固定隔离策略。"""
    args = validate_argv(argv)
    if type(timeout) is not int or not 1 <= timeout <= 5:
        raise ValueError("invalid_command_timeout")
    return (
        json.dumps({"argv": args, "timeout_seconds": timeout}, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )


def read_request(raw: bytes) -> tuple[list[str], int]:
    """拒绝额外字段和残缺帧，避免另一条通道覆盖挂载或启动选项。"""
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid_command_frame")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"argv", "timeout_seconds"}:
        raise ValueError("invalid_command_request")
    request(value["argv"], value["timeout_seconds"])
    return value["argv"], value["timeout_seconds"]

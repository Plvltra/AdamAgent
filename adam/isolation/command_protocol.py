"""原生命令隔离的有界协议；宿主与 Linux 启动器共用，兼容 Python 3.8。"""

from __future__ import annotations

import base64
import json

from adam.contracts.command_inputs import CommandInput, validate_inputs

POLICY = "bubblewrap-native-single-process-v1"
MAX_ARGV_BYTES = 65536
MAX_REQUEST_BYTES = 3145728
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
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_ARGV_BYTES - 256:
        raise ValueError("command_argv_too_large")
    return result


def request(argv: object, timeout: object, inputs: tuple[CommandInput, ...] = ()) -> bytes:
    """编码启动请求；墙钟上限只可调低，不允许覆盖固定隔离策略。"""
    args = validate_argv(argv)
    if type(timeout) is not int or not 1 <= timeout <= 5:
        raise ValueError("invalid_command_timeout")
    validate_inputs(inputs)
    files = [
        {
            "name": item.name,
            "version": item.version,
            "data": base64.b64encode(item.data).decode("ascii"),
        }
        for item in inputs
    ]
    raw = (
        json.dumps(
            {"argv": args, "timeout_seconds": timeout, "inputs": files},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("isolated_input_wire_limit")
    return raw


def read_request(raw: bytes) -> tuple[list[str], int, tuple[CommandInput, ...]]:
    """拒绝额外字段和残缺帧，避免另一条通道覆盖挂载或启动选项。"""
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid_command_frame")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) - {"argv", "timeout_seconds", "inputs"}
        or not {"argv", "timeout_seconds"} <= value.keys()
    ):
        raise ValueError("invalid_command_request")
    files = value.get("inputs", [])
    if not isinstance(files, list) or len(files) > 16:
        raise ValueError("invalid_isolated_inputs")
    inputs = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "version", "data"}
            or any(not isinstance(part, str) for part in item.values())
        ):
            raise ValueError("invalid_isolated_input_fields")
        inputs.append(
            CommandInput(
                item["name"], base64.b64decode(item["data"], validate=True), item["version"]
            )
        )
    result = tuple(inputs)
    request(value["argv"], value["timeout_seconds"], result)
    return value["argv"], value["timeout_seconds"], result

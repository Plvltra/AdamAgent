"""原生命令隔离的有界协议；宿主与 Linux 启动器共用，兼容 Python 3.8。"""

from __future__ import annotations

import base64
import json

from adam.contracts.command_inputs import CommandInput, validate_inputs
from adam.contracts.command_inputs import validate_argv as validate_argv

POLICY = "bubblewrap-native-single-process-v1"
MAX_REQUEST_BYTES = 3145728
MAX_RESPONSE_BYTES = 524288
MAX_OUTPUT_BYTES = 65536


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

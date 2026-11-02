"""命令事实的字段校验；隔离输入来源只能在批准/开始时确定，结束记录不能改写。"""

import re
from typing import Any

from adam.contracts.command_inputs import validate_argv, validate_names
from adam.contracts.execution import ISOLATED_COMMAND_TOOL


def validate_start(name: str, payload: dict[str, Any]) -> None:
    """校验新隔离入口的固定环境与版本来源；旧宿主日志保持原有字段兼容。"""
    if name != ISOLATED_COMMAND_TOOL:
        if payload.get("environment", "host") != "host":
            raise ValueError("host command cannot claim isolation")
        return
    validate_argv(payload["argv"])
    distribution = payload.get("distribution")
    files = payload.get("input_files")
    if (
        payload.get("environment") != "isolated"
        or payload["cwd"] != "/work"
        or type(payload.get("timeout_seconds")) is not int
        or not 1 <= payload["timeout_seconds"] <= 5
        or not isinstance(distribution, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", distribution) is None
        or not isinstance(files, list)
    ):
        raise ValueError("invalid isolated command environment")
    for item in files:
        if (
            not isinstance(item, dict)
            or item.keys() != {"name", "source", "version"}
            or any(not isinstance(value, str) for value in item.values())
            or not item["source"]
            or re.fullmatch(r"[0-9a-f]{64}", item["version"]) is None
        ):
            raise ValueError("invalid isolated command input source")
    validate_names(tuple(item["name"] for item in files))


def validate_finish(previous: dict[str, Any], payload: dict[str, Any]) -> None:
    """结束事件保留原环境和输入来源，正常结果须同时有真实退出码及一致的停止原因。"""
    for key in ("argv", "cwd", "timeout_seconds", "environment", "distribution", "input_files"):
        if key in payload and payload[key] != previous.get(key):
            raise ValueError("command finish changed approved identity")
    allowed = {"completed", "timed_out", "start_failed", "interrupted"}
    isolated = previous.get("environment") == "isolated"
    if isolated:
        allowed = {"completed", "timed_out", "output_limited", "interrupted"}
    status = payload.get("status")
    if status not in allowed:
        raise ValueError("invalid command status")
    if payload.get("exit_code") is not None and type(payload["exit_code"]) is not int:
        raise ValueError("invalid exit code")
    if isolated and status != "interrupted":
        expected = {
            "completed": "exited",
            "timed_out": "wall_limit",
            "output_limited": "output_limit",
        }
        if (
            type(payload.get("exit_code")) is not int
            or payload.get("reason") != expected[status]
            or payload.get("policy") != "bubblewrap-native-single-process-v1"
        ):
            raise ValueError("inconsistent isolated command outcome")
        if "invalid_utf8" in payload and (
            type(payload["invalid_utf8"]) is not bool
            or (payload["invalid_utf8"] and payload.get("truncated") is not True)
        ):
            raise ValueError("isolated output decoding loss must remain incomplete")

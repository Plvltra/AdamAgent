"""隔离命令的不可变输入字节；不携带宿主挂载路径，兼容 Linux Python 3.8。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

MAX_FILES = 16
MAX_FILE_BYTES = 1048576
MAX_TOTAL_BYTES = 2097152


@dataclass(frozen=True)
class CommandInput:
    """只读虚拟文件与实际字节 SHA256；来源授权由宿主负责，不能把该对象当授权。"""

    name: str
    data: bytes = field(repr=False)
    version: str


def validate_names(names: tuple[str, ...]) -> None:
    """只接受相对 POSIX 文件名，拒绝逃逸、规范化别名及文件/目录前缀冲突。"""
    if (
        len(names) > MAX_FILES
        or any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("isolated_input_count_or_duplicate")
    for name in names:
        if (
            not name
            or "\\" in name
            or ":" in name
            or "\0" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or len(name.encode("utf-8")) > 1024
            or any(len(part.encode("utf-8")) > 255 for part in name.split("/"))
        ):
            raise ValueError("invalid_isolated_input_name")
        if any(str(parent) in names for parent in PurePosixPath(name).parents):
            raise ValueError("isolated_input_path_conflict")


def validate_inputs(inputs: tuple[CommandInput, ...]) -> None:
    """校验整批大小和实际摘要；数据被替换、伪造版本或重复映射时不能启动命令。"""
    if not isinstance(inputs, tuple) or any(not isinstance(item, CommandInput) for item in inputs):
        raise ValueError("invalid_isolated_inputs")
    validate_names(tuple(item.name for item in inputs))
    total = 0
    for item in inputs:
        if (
            not isinstance(item.data, bytes)
            or len(item.data) > MAX_FILE_BYTES
            or item.version != hashlib.sha256(item.data).hexdigest()
        ):
            raise ValueError("invalid_isolated_input_content")
        total += len(item.data)
    if total > MAX_TOTAL_BYTES:
        raise ValueError("isolated_input_total_limit")

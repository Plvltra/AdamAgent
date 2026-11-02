"""将显式文件选择变成有界输入快照；沿用原路径权限，不扫描目录或挂载项目。"""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_inputs import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    CommandInput,
    validate_inputs,
    validate_names,
)
from adam.contracts.errors import ToolError
from adam.tool_calls.policy import PathPolicy


@dataclass(frozen=True)
class InputSource:
    """批准时可展示的来源，原始选择路径用于再次授权，解析路径用于检测指向变化。"""

    name: str
    requested_path: str
    path: Path
    version: str


@dataclass(frozen=True)
class CapturedInputs:
    """不可变文件快照及来源；审批后 verify 通过才能将原快照交给隔离后端。"""

    files: tuple[CommandInput, ...]
    sources: tuple[InputSource, ...]

    def verify(self, policy: PathPolicy, cancel: CancellationToken | None = None) -> None:
        """重新授权并读取源版本；变化后拒绝旧批准，检查本身不会重建或悄悄替换快照。"""
        token = cancel or CancellationToken()
        for source in self.sources:
            path, data = _read(policy, source.requested_path, MAX_FILE_BYTES, token)
            if path != source.path or hashlib.sha256(data).hexdigest() != source.version:
                raise ToolError("隔离输入来源或内容已变化，请重新准备并批准。")


def capture_inputs(
    policy: PathPolicy,
    selected: dict[str, str],
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    cancel: CancellationToken | None = None,
) -> CapturedInputs:
    """按虚拟名称排序读取明确选择的普通文件，返回实际字节、版本和宿主来源。

    selected 将 /work 下相对文件名映射到原工具路径。最多16个、单个1MiB、合计2MiB；
    max_file_bytes 只可调低。任何权限、读取或数量失败都不交付部分快照；不解码文件。
    """
    token = cancel or CancellationToken()
    token.check()
    if (
        not isinstance(selected, dict)
        or any(
            not isinstance(name, str) or not isinstance(path, str)
            for name, path in selected.items()
        )
        or type(max_file_bytes) is not int
        or not 1 <= max_file_bytes <= MAX_FILE_BYTES
    ):
        raise ToolError("隔离输入必须是虚拟名称到源文件路径的映射，单文件限制须为1～1MiB。")
    try:
        validate_names(tuple(selected))
    except ValueError as error:
        raise ToolError("隔离输入名称无效、重复、前缀冲突或超过16个文件。") from error
    files, sources = [], []
    total = 0
    for name, requested in sorted(selected.items()):
        path, data = _read(policy, requested, max_file_bytes, token)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ToolError("隔离输入总量超过2MiB；未执行命令。")
        version = hashlib.sha256(data).hexdigest()
        files.append(CommandInput(name, data, version))
        sources.append(InputSource(name, requested, path, version))
    batch = CapturedInputs(tuple(files), tuple(sources))
    validate_inputs(batch.files)
    batch.verify(policy, token)
    return batch


def _read(
    policy: PathPolicy,
    requested: str,
    limit: int,
    token: CancellationToken,
) -> tuple[Path, bytes]:
    """比较句柄前后与路径元数据；版本来自实际字节，元数据检查不是外部写入锁。"""
    token.check()
    try:
        path = policy.resolve(requested)
        if not path.is_file():
            raise ToolError("隔离输入只接受已有普通文件。")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ToolError("隔离输入不是普通文件或单文件大小超限。")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        token.check()
        if (
            len(data) > limit
            or _stamp(before) != _stamp(after)
            or policy.resolve(requested) != path
            or _stamp(path.stat()) != _stamp(after)
        ):
            raise ToolError("隔离输入在读取时变化；未交付快照。")
        return path, data
    except OSError as error:
        raise ToolError("隔离输入读取失败；未执行命令。") from error


def _stamp(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

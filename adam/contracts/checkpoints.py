"""编辑检查点的不可变契约；保存原字节和来源，不代表修改已成功。"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class EditCheckpoint:
    """修改前的可靠副本；target_version 是预期结果，成功事实仍以编辑日志为准。

    path 仅为来源元数据，本契约不读取目标文件。original 保留 BOM、换行和末尾字节，
    before_version 必须对应这些字节；无效来源或版本在构造时抛出 ValueError。
    """

    session_id: str
    edit_id: str
    turn_id: str
    request_id: str
    tool_call_id: str
    path: Path
    original: bytes
    before_version: str
    target_version: str

    def __post_init__(self) -> None:
        validate_checkpoint_ids(self.session_id, self.edit_id)
        for value in (self.turn_id, self.request_id):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
                raise ValueError("invalid checkpoint source")
        if (
            not isinstance(self.tool_call_id, str)
            or not self.tool_call_id
            or "\0" in self.tool_call_id
            or len(self.tool_call_id.encode("utf-8")) > 1024
        ):
            raise ValueError("invalid checkpoint tool id")
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or ".." in self.path.parts
            or "\0" in str(self.path)
            or len(str(self.path).encode("utf-8")) > 32768
        ):
            raise ValueError("invalid checkpoint target path")
        for version in (self.before_version, self.target_version):
            if not isinstance(version, str) or re.fullmatch(r"[0-9a-f]{64}", version) is None:
                raise ValueError("invalid checkpoint version")
        if (
            not isinstance(self.original, bytes)
            or hashlib.sha256(self.original).hexdigest() != self.before_version
            or self.before_version == self.target_version
        ):
            raise ValueError("checkpoint bytes do not match versions")


@dataclass(frozen=True)
class UndoResult:
    """已落盘的用户撤销结果；失败或未知结果使用公共异常，不能返回成功。"""

    undo_id: str
    edit_id: str
    path: Path
    status: Literal["applied", "denied"]


def validate_checkpoint_ids(session_id: str, edit_id: str) -> None:
    """内部文件名只接受程序生成的短会话 ID 与编辑 ID，禁止把任意路径拼进存储目录。"""
    for value, width in ((session_id, 8), (edit_id, 32)):
        if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is None:
            raise ValueError("invalid checkpoint identity")

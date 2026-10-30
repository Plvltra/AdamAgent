"""长工具文本的不可变引用与读取范围；来源是关联数据，本模块不访问文件。"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactSource:
    """附件所属会话与实际工具调用；调用方仍须核对这些 ID 对应真实执行记录。"""

    session_id: str
    turn_id: str
    request_id: str
    tool_call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        for value, width in (
            (self.session_id, 8),
            (self.turn_id, 32),
            (self.request_id, 32),
        ):
            _hex_identifier(value, width)
        for value, limit in ((self.tool_call_id, 1024), (self.tool_name, 256)):
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\0" in value
                or len(value.encode("utf-8")) > limit
            ):
                raise ValueError("invalid artifact source")


@dataclass(frozen=True)
class ArtifactRef:
    """固定内容版本、来源和保存范围；total_bytes 为 None 表示原输出总量未知。

    saved_chars 按 Unicode 字符计数，分页不使用字节偏移。complete 只有在原输出
    已完整取得且全部保存时为 True；到达附件末尾不等于取得全部原输出。
    """

    artifact_id: str
    source: ArtifactSource
    version: str
    saved_bytes: int
    saved_chars: int
    total_bytes: int | None
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source, ArtifactSource):
            raise ValueError("invalid artifact source type")
        validate_artifact_identity(self.source.session_id, self.artifact_id)
        _hex_identifier(self.version, 64)
        if any(
            type(value) is not int or value < 0 for value in (self.saved_bytes, self.saved_chars)
        ):
            raise ValueError("invalid artifact size")
        if not self.saved_chars <= self.saved_bytes <= 4 * self.saved_chars:
            raise ValueError("invalid UTF-8 size")
        if self.total_bytes is not None and (
            type(self.total_bytes) is not int or self.total_bytes < self.saved_bytes
        ):
            raise ValueError("invalid artifact total")
        if type(self.complete) is not bool or self.complete != (
            self.total_bytes == self.saved_bytes
        ):
            raise ValueError("invalid artifact completeness")


@dataclass(frozen=True)
class ArtifactPage:
    """某份已保存文本的字符范围；next_offset 为 None 仅表示附件已读完。"""

    reference: ArtifactRef
    offset: int
    text: str
    next_offset: int | None


def decode_artifact_reference(value: object) -> ArtifactRef:
    """从日志或附件元数据还原同一引用契约；多余或缺失字段均拒绝。"""
    if not isinstance(value, dict):
        raise ValueError("invalid artifact reference")
    try:
        metadata = dict(value)
        source = ArtifactSource(**metadata.pop("source"))
        return ArtifactRef(source=source, **metadata)
    except (KeyError, TypeError) as error:
        raise ValueError("invalid artifact reference fields") from error


def validate_artifact_identity(session_id: str, artifact_id: str) -> None:
    """路径只使用程序 ID，不接受用户传入的任意相对路径。"""
    _hex_identifier(session_id, 8)
    _hex_identifier(artifact_id, 32)


def _hex_identifier(value: str, width: int) -> None:
    """拒绝空值、路径字符及不符合本类型长度的标识。"""
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is None:
        raise ValueError("invalid artifact identifier")

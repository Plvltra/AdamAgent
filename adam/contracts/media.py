"""二进制证据的不可变引用；只描述来源和完整性，不携带路径或媒体正文。"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from adam.contracts.artifacts import ArtifactSource, validate_artifact_identity


@dataclass(frozen=True)
class MediaSource:
    """本会话用户输入或真实工具来源；input 不得夹带模型请求或工具身份。"""

    session_id: str
    turn_id: str
    kind: str
    request_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        validate_artifact_identity(self.session_id, self.turn_id)
        if self.kind == "tool":
            if (
                not isinstance(self.request_id, str)
                or not isinstance(self.tool_call_id, str)
                or not isinstance(self.tool_name, str)
            ):
                raise ValueError("missing media tool source")
            ArtifactSource(
                self.session_id, self.turn_id, self.request_id, self.tool_call_id, self.tool_name
            )
        elif self.kind != "input" or any(
            value is not None for value in (self.request_id, self.tool_call_id, self.tool_name)
        ):
            raise ValueError("invalid media source kind or fields")


@dataclass(frozen=True)
class MediaRef:
    """完整二进制正文的固定 SHA-256 版本；MIME 是声明，不保证内容可被相应解码器处理。"""

    media_id: str
    source: MediaSource
    mime_type: str
    size_bytes: int
    version: str
    created_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.source, MediaSource):
            raise ValueError("invalid media source")
        validate_artifact_identity(self.source.session_id, self.media_id)
        if not isinstance(self.version, str) or re.fullmatch(r"[0-9a-f]{64}", self.version) is None:
            raise ValueError("invalid media version")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("invalid media size")
        validate_mime_type(self.mime_type)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported media reference version")
        try:
            if not isinstance(self.created_at, str) or len(self.created_at) > 64:
                raise ValueError("invalid media timestamp size")
            timestamp = datetime.fromisoformat(self.created_at)
            if timestamp.utcoffset() != timedelta(0):
                raise ValueError("media timestamp must be UTC")
        except (TypeError, ValueError) as error:
            raise ValueError("invalid media timestamp") from error


@dataclass(frozen=True)
class MediaContent:
    """已验证引用和完整媒体字节；仅宿主端口使用，不直接序列化到 JSONL 或模型消息。"""

    reference: MediaRef
    data: bytes


def validate_mime_type(value: str) -> None:
    """接受不带参数的小写具体 MIME；拒绝通配符、控制字符和多余参数。"""
    token = r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
    if not isinstance(value, str) or re.fullmatch(token + "/" + token, value) is None:
        raise ValueError("invalid media MIME type")


def decode_media_reference(value: object) -> MediaRef:
    """严格还原日志或旁路文件引用；未知/缺失字段及坏来源均拒绝。"""
    if not isinstance(value, dict) or value.keys() != {
        "media_id",
        "source",
        "mime_type",
        "size_bytes",
        "version",
        "created_at",
        "schema_version",
    }:
        raise ValueError("invalid media reference fields")
    try:
        fields: dict[str, Any] = dict(value)
        source = fields.pop("source")
        if not isinstance(source, dict) or source.keys() != {
            "session_id",
            "turn_id",
            "kind",
            "request_id",
            "tool_call_id",
            "tool_name",
        }:
            raise ValueError("invalid media source fields")
        return MediaRef(source=MediaSource(**source), **fields)
    except TypeError as error:
        raise ValueError("invalid media reference values") from error

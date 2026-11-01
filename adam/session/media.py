"""会话隔离的完整二进制证据；有界元数据与原字节在同一个文件中原子发布。"""

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.contracts.artifacts import validate_artifact_identity
from adam.contracts.errors import StorageError
from adam.contracts.media import (
    MediaContent,
    MediaRef,
    MediaSource,
    decode_media_reference,
    validate_mime_type,
)

_MAGIC = b"ADAM-MEDIA\x00\x01\n"
_METADATA_LIMIT = 32768


class MediaStore:
    """存储完整而不可覆盖的二进制版本；来源授权由 SessionMedia 核对，文件存在不是授权。"""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 10485760,
        max_session_bytes: int = 67108864,
        max_items: int = 128,
    ) -> None:
        """注入正文/会话字节与数量上限；不读取配置，不截断图片或音频来满足配额。"""
        for value, upper in (
            (max_bytes, 100000000),
            (max_session_bytes, 1073741824),
            (max_items, 4096),
        ):
            if type(value) is not int or not 0 < value <= upper:
                raise ValueError("invalid media limit")
        self.root = root.absolute()
        self.max_bytes, self.max_session_bytes, self.max_items = (
            max_bytes,
            max_session_bytes,
            max_items,
        )

    def save(
        self, source: MediaSource, data: bytes, mime_type: str, *, media_id: str | None = None
    ) -> MediaRef:
        """原子保存完整字节并返回真实引用；超限、锁冲突或 IO 失败不返回引用，不覆盖旧 ID。"""
        if not isinstance(source, MediaSource) or not isinstance(data, bytes):
            raise ValueError("media source and immutable bytes required")
        validate_mime_type(mime_type)
        if len(data) > self.max_bytes:
            raise StorageError("媒体正文超过单份上限，未保存，不提供截断引用。")
        identity = secrets.token_hex(16) if media_id is None else media_id
        with self._locked(source.session_id, identity) as path:
            if path.exists():
                raise StorageError("媒体 ID 已存在，拒绝覆盖固定版本。")
            used = self._usage(path.parent, source.session_id)
            if used + len(data) > self.max_session_bytes:
                raise StorageError("媒体会话字节配额不足，未保存。")
            reference = MediaRef(
                identity,
                source,
                mime_type,
                len(data),
                hashlib.sha256(data).hexdigest(),
                datetime.now(UTC).isoformat(),
            )
            metadata = json.dumps(asdict(reference), ensure_ascii=False).encode("utf-8")
            if len(metadata) > _METADATA_LIMIT:
                raise StorageError("媒体元数据超过上限，未保存。")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._publish(path, metadata, data)
            return reference

    def restore(self, content: MediaContent) -> None:
        """复制已验证证据并保留完整身份与时间；不授予会话读取权限，不覆盖不同证据。

        供录制归档使用，源读取端口先验证所属会话；已有相同文件时幂等返回。
        正文或元数据不符、超限与发布失败时停止，不留下可交付的替代引用。
        """
        if not isinstance(content, MediaContent) or not isinstance(content.reference, MediaRef):
            raise ValueError("verified media content required")
        reference, data = content.reference, content.data
        if (
            not isinstance(data, bytes)
            or len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.version
        ):
            raise StorageError("归档媒体正文与固定引用不符，未复制。")
        if len(data) > self.max_bytes:
            raise StorageError("归档媒体正文超过单份上限，未复制。")
        with self._locked(reference.source.session_id, reference.media_id) as path:
            if path.exists():
                if self._read(path, reference.source.session_id, reference.media_id) != content:
                    raise StorageError("归档媒体 ID 已有不同证据，拒绝覆盖。")
                return
            used = self._usage(path.parent, reference.source.session_id)
            if used + len(data) > self.max_session_bytes:
                raise StorageError("归档媒体字节配额不足，未复制。")
            metadata = json.dumps(asdict(reference), ensure_ascii=False).encode("utf-8")
            if len(metadata) > _METADATA_LIMIT:
                raise StorageError("归档媒体元数据超过上限，未复制。")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._publish(path, metadata, data)

    def read_frozen(self, reference: MediaRef) -> MediaContent:
        """只读核对冻结副本，不建目录或锁文件；文件存在不代表会话来源已获授权。

        证据单文件原子发布，读取完整快照即可核对；用于离线归档验证而非活动来源授权。
        """
        try:
            path = self._contained(
                self.root / reference.source.session_id / f"{reference.media_id}.media"
            )
            content = self._read(path, reference.source.session_id, reference.media_id)
            if content.reference != reference:
                raise StorageError("冻结媒体元数据与引用不一致，拒绝读取。")
            return content
        except OSError as error:
            raise StorageError("冻结媒体缺失或不可读。") from error

    def describe(self, session_id: str, media_id: str) -> MediaRef:
        """校验完整旁路文件，返回元数据；缺失、损坏和来源不符抛出 StorageError。"""
        with self._locked(session_id, media_id) as path:
            return self._read(path, session_id, media_id).reference

    def read(self, session_id: str, media_id: str, *, expected_version: str) -> MediaContent:
        """验证身份、完整大小和摘要后返回全部字节；不寻找其他会话或猜测替代版本。"""
        with self._locked(session_id, media_id) as path:
            content = self._read(path, session_id, media_id)
            if content.reference.version != expected_version:
                raise StorageError("媒体版本与引用不一致，拒绝读取。")
            return content

    @contextmanager
    def _locked(self, session_id: str, media_id: str) -> Iterator[Path]:
        """身份与真实路径核对后持有会话文件锁；拒绝文件、目录及跨会话链接重定向。"""
        validate_artifact_identity(session_id, media_id)
        try:
            self._contained(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self._contained(self.root / ".locks" / f"{session_id}.lock")
            lock_path.parent.mkdir(exist_ok=True)
            with FileLock(lock_path, timeout=0):
                yield self._contained(self.root / session_id / f"{media_id}.media")
        except Timeout:
            raise StorageError("媒体会话正在被其他操作访问，未重试。") from None
        except OSError as error:
            raise StorageError("媒体文件操作失败，请检查路径、权限及磁盘空间。") from error

    def _contained(self, path: Path) -> Path:
        """要求所有路径保持在原始媒体根内，链接到同一根内的其他会话也拒绝。"""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as error:
            raise StorageError("媒体路径无法解析，拒绝访问。") from error
        if resolved != path or not resolved.is_relative_to(self.root):
            raise StorageError("媒体路径重定向或超出所属目录，拒绝访问。")
        return path

    def _usage(self, directory: Path, session_id: str) -> int:
        """持锁核算实际已发布文件；坏文件不跳过，孤立临时文件不自动发布。"""
        if not directory.exists():
            return 0
        used = items = 0
        for visited, path in enumerate(directory.iterdir(), 1):
            if visited > self.max_items * 2 + 32:
                raise StorageError("媒体目录项过多，请检查遗留文件。")
            if path.suffix != ".media":
                continue
            items += 1
            if items >= self.max_items:
                raise StorageError("会话媒体数量已达上限，未保存。")
            used += self._read(self._contained(path), session_id, path.stem).reference.size_bytes
        return used

    def _read(self, path: Path, session_id: str, media_id: str) -> MediaContent:
        """固定上限读取头部和正文；磁盘自报的长度不得决定无界分配。"""
        try:
            with path.open("rb") as file:
                if file.read(len(_MAGIC)) != _MAGIC:
                    raise ValueError("unsupported media file")
                width = file.read(4)
                if len(width) != 4:
                    raise ValueError("missing metadata length")
                size = int.from_bytes(width, "big")
                if not 0 < size <= _METADATA_LIMIT:
                    raise ValueError("invalid metadata length")
                metadata = file.read(size)
                if len(metadata) != size:
                    raise ValueError("incomplete media metadata")
                reference = decode_media_reference(
                    json.loads(metadata.decode("utf-8"), object_pairs_hook=_unique_fields)
                )
                data = file.read(self.max_bytes + 1)
            if (
                reference.source.session_id != session_id
                or reference.media_id != media_id
                or reference.size_bytes != len(data)
                or len(data) > self.max_bytes
                or reference.version != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("media identity or content mismatch")
            return MediaContent(reference, data)
        except (ValueError, UnicodeError, TypeError, RecursionError) as error:
            raise StorageError("媒体内容损坏、来源不符或格式不受支持，拒绝读取。") from error

    def _publish(self, path: Path, metadata: bytes, data: bytes) -> None:
        """同目录临时文件 fsync 后原子替换；单文件发布避免正文与引用半提交。"""
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".media-", suffix=".tmp", dir=path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(_MAGIC)
                file.write(len(metadata).to_bytes(4, "big"))
                file.write(metadata)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 临时文件不是有效证据，清理失败不覆盖原始发布故障。
                    pass


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate media field")
        result[key] = value
    return result

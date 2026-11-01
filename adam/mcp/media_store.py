"""MCP 二进制结果的有界本地引用；只保存原始字节，不解码媒体或执行文件。"""

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout

from adam.contracts.errors import ToolError

MAX_MEDIA_BYTES = 1_048_576


@dataclass(frozen=True)
class MediaSource:
    """宿主生成的服务身份和结果位置；不包含认证头，也不把服务器声明当执行证据。"""

    server: str
    server_id: str
    kind: Literal["tool", "resource", "prompt"]
    name: str
    part: int

    def __post_init__(self) -> None:
        if not isinstance(self.server, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,15}", self.server
        ):
            raise ToolError("MCP 媒体来源的服务别名无效。")
        _hex(self.server_id)
        if self.kind not in {"tool", "resource", "prompt"}:
            raise ToolError("MCP 媒体来源类型无效。")
        if (
            not isinstance(self.name, str)
            or not 1 <= len(self.name) <= 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in self.name)
            or type(self.part) is not int
            or not 0 <= self.part < 16
        ):
            raise ToolError("MCP 媒体来源名称或结果位置无效。")


@dataclass(frozen=True)
class MediaReference:
    """固定内容版本与来源；mime_type 仅是声明，不能据此声称内容已被解码或理解。"""

    media_id: str
    version: str
    size_bytes: int
    mime_type: str
    source: MediaSource
    format: Literal["mcp_binary_v1"] = "mcp_binary_v1"

    def __post_init__(self) -> None:
        _hex(self.media_id)
        _hex(self.version)
        if (
            type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= MAX_MEDIA_BYTES
            or not isinstance(self.source, MediaSource)
            or self.format != "mcp_binary_v1"
        ):
            raise ToolError("MCP 媒体引用格式或大小无效。")
        _mime(self.mime_type)
        if self.media_id != _identity(self.version, self.size_bytes, self.mime_type, self.source):
            raise ToolError("MCP 媒体引用身份与内容/来源不一致。")


@dataclass(frozen=True)
class PreparedMedia:
    """已解码、尚未保存的有限字节；只能在整个结果验证通过后批量发布。"""

    reference: MediaReference
    data: bytes = field(repr=False)


def prepare_media(blob: str, mime_type: str | None, source: MediaSource) -> PreparedMedia:
    """在 IO 前严格解码最多1 MiB的 base64，计算固定引用；不把字节放进引用或异常文本。"""
    mime = mime_type if mime_type is not None else "application/octet-stream"
    _mime(mime)
    if not isinstance(source, MediaSource):
        raise ToolError("MCP 媒体来源无效。")
    if not isinstance(blob, str) or len(blob) > ((MAX_MEDIA_BYTES + 2) // 3) * 4:
        raise ToolError("MCP 媒体编码超过1 MiB内容上限。")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ToolError("MCP 媒体不是合法 base64；未保存。") from error
    if len(raw) > MAX_MEDIA_BYTES:
        raise ToolError("MCP 媒体超过1 MiB；未截断保存。")
    version = hashlib.sha256(raw).hexdigest()
    reference = MediaReference(
        _identity(version, len(raw), mime, source), version, len(raw), mime, source
    )
    return PreparedMedia(reference, raw)


def decode_reference(value: object) -> MediaReference:
    """还原宿主记录中的完整引用；拒绝额外字段及路径字符，不把外部 URI 当作本地路径。"""
    if not isinstance(value, dict) or set(value) != {
        "media_id",
        "version",
        "size_bytes",
        "mime_type",
        "source",
        "format",
    }:
        raise ToolError("MCP 媒体引用必须包含完整且已知的字段。")
    try:
        fields = dict(value)
        source = MediaSource(**fields.pop("source"))
        return MediaReference(source=source, **fields)
    except (KeyError, TypeError) as error:
        raise ToolError("MCP 媒体引用字段无效。") from error


class MCPMediaStore:
    """发布完整二进制文件；相同内容和来源复用引用，损坏或缺失时不重新请求远端。"""

    def __init__(
        self, root: Path, *, max_total_bytes: int = 16_777_216, max_items: int = 128
    ) -> None:
        """注入宿主选择的保留目录与总量限制；构造不创建目录，限额不足不驱逐旧引用。"""
        if (
            type(max_total_bytes) is not int
            or not 1 <= max_total_bytes <= 268_435_456
            or type(max_items) is not int
            or not 1 <= max_items <= 1024
        ):
            raise ValueError("invalid MCP media cache limit")
        self.root = root.resolve()
        self.max_total_bytes, self.max_items = max_total_bytes, max_items

    def save_many(self, items: Sequence[PreparedMedia]) -> list[MediaReference]:
        """验证全部内容并预检配额后逐项原子发布，返回与输入顺序相同的实际引用。

        上限为16项；同一批重复项只占一份配额。IO失败不返回部分成功，已发布的
        文件可能成为孤立缓存但不能自动进入聊天记录。中断临时文件也计入配额，不自动恢复。
        """
        if not 1 <= len(items) <= 16:
            raise ToolError("MCP 媒体批次必须是1～16项。")
        unique: dict[str, PreparedMedia] = {}
        for item in items:
            if (
                not isinstance(item, PreparedMedia)
                or not isinstance(item.reference, MediaReference)
                or not isinstance(item.data, bytes)
            ):
                raise ToolError("MCP 媒体待保存内容无效。")
            reference = decode_reference(asdict(item.reference))
            if (
                len(item.data) != reference.size_bytes
                or hashlib.sha256(item.data).hexdigest() != reference.version
            ):
                raise ToolError("MCP 媒体内容与引用不符；未保存。")
            unique[reference.media_id] = item
        with self._locked():
            pending = []
            for identity, item in unique.items():
                if self._path(identity).exists():
                    self._read(item.reference)
                else:
                    pending.append(item)
            count, size = self._usage()
            if (
                count + len(pending) > self.max_items
                or size + sum(len(item.data) for item in pending) > self.max_total_bytes
            ):
                raise ToolError("MCP 媒体缓存配额不足；未保存本批新内容，也未删除旧引用。")
            for item in pending:
                self._publish(item.reference.media_id, item.data)
        return [item.reference for item in items]

    def read(self, value: MediaReference | dict[str, Any]) -> bytes:
        """由持有日志引用的宿主读取完整字节；核对来源身份、大小和摘要，不注册为模型工具。"""
        reference = decode_reference(asdict(value) if isinstance(value, MediaReference) else value)
        with self._locked():
            return self._read(reference)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """所有实例共用目录锁；底层 IO 失败只给出边界错误，不回显媒体内容。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.resolve() != self.root:
                raise ToolError("MCP 媒体缓存目录已改变；拒绝继续。")
            with FileLock(self.root / ".lock", timeout=2):
                yield
        except (OSError, Timeout) as error:
            raise ToolError("MCP 媒体缓存操作失败或正被占用；未自动重试。") from error

    def _path(self, identity: str) -> Path:
        _hex(identity)
        path = self.root / (identity + ".bin")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ToolError("MCP 媒体文件路径异常；拒绝访问。")
        return path

    def _read(self, reference: MediaReference) -> bytes:
        path = self._path(reference.media_id)
        if not path.is_file():
            raise ToolError("MCP 媒体缓存不是可读普通文件；未重新请求远端。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_MEDIA_BYTES + 1)
        if len(raw) != reference.size_bytes or hashlib.sha256(raw).hexdigest() != reference.version:
            raise ToolError("MCP 媒体缓存缺失完整内容或版本不符；未重新请求远端。")
        return raw

    def _usage(self) -> tuple[int, int]:
        """中断残留和其他普通文件也占配额；不从文件名推断可以删除或恢复的内容。"""
        count, total = 0, 0
        for path in self.root.iterdir():
            if path.name == ".lock":
                continue
            if path.is_symlink() or not path.is_file():
                raise ToolError("MCP 媒体缓存存在异常条目；拒绝继续保存。")
            count += 1
            total += path.stat().st_size
            if count > self.max_items or total > self.max_total_bytes:
                raise ToolError("MCP 媒体缓存已超出配额，请由宿主清理确认不再需要的内容。")
        return count, total

    def _publish(self, identity: str, data: bytes) -> None:
        """先同步临时文件再原子替换；仅在持锁且目标不存在时调用。"""
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".pending-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(identity))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _identity(version: str, size: int, mime: str, source: MediaSource) -> str:
    value = {"version": version, "size_bytes": size, "mime_type": mime, "source": asdict(source)}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hex(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ToolError("MCP 媒体标识必须是 SHA-256。")


def _mime(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 200
        or re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value) is None
    ):
        raise ToolError("MCP 媒体 MIME 类型无效。")

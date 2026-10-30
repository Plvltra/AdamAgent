"""按会话隔离的有界文本附件；完整元数据与内容一起原子发布。"""

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.contracts.artifacts import (
    ArtifactPage,
    ArtifactRef,
    ArtifactSource,
    validate_artifact_identity,
)
from adam.contracts.errors import StorageError


class ArtifactStore:
    """附件写入后不可覆盖；保存范围和原输出完整性分别保留，不把截断伪装成完整。"""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 1_048_576,
        max_session_bytes: int = 16_777_216,
        max_items: int = 128,
        max_read_chars: int = 4000,
    ) -> None:
        """注入单份、会话总文本字节、数量和单页字符上限；不在存储层读取配置。

        字节配额只计算已发布的 UTF-8 正文；JSON 元数据另受编码大小和文件数量限制。
        孤立临时文件不会成为附件，也不会在重启时自动发布。
        """
        for value, upper in (
            (max_bytes, 100_000_000),
            (max_session_bytes, 1_073_741_824),
            (max_items, 4096),
            (max_read_chars, 1_000_000),
        ):
            if type(value) is not int or not 0 < value <= upper:
                raise ValueError("invalid artifact limit")
        self.root = root.absolute()
        self.max_bytes, self.max_session_bytes = max_bytes, max_session_bytes
        self.max_items, self.max_read_chars = max_items, max_read_chars
        # JSON 控制字符最多占六字节；来源字段有独立上限，读取不依赖文件自报大小。
        self._encoded_limit = max_bytes * 6 + 65536

    def save(
        self,
        source: ArtifactSource,
        text: str,
        *,
        artifact_id: str | None = None,
        source_complete: bool = True,
    ) -> ArtifactRef:
        """可靠保存全文或有效 UTF-8 前缀，返回实际可读的引用；已存在 ID 一律拒绝。

        source_complete=False 表示上游已丢弃部分输出，总量保持未知。配额不足时
        complete=False；连一个完整字符都放不下、数量超限或 IO 失败则抛出 StorageError。
        来源必须由调用方从真实工具记录取得，本方法不创建或校验聊天轮次。
        """
        if not isinstance(source, ArtifactSource) or type(source_complete) is not bool:
            raise ValueError("invalid artifact source")
        if not isinstance(text, str):
            raise ValueError("artifact content must be text")
        try:
            raw = text.encode("utf-8")
        except UnicodeError as error:
            raise StorageError("附件必须是有效 UTF-8 文本，未保存。") from error
        identity = artifact_id if artifact_id is not None else secrets.token_hex(16)
        with self._locked(source.session_id, identity) as path:
            if path.exists():
                raise StorageError("附件 ID 已存在，拒绝覆盖固定内容。")
            used = self._usage(path.parent, source.session_id)
            capacity = min(self.max_bytes, self.max_session_bytes - used)
            # 原文已严格编码，ignore 只去掉上限切开的最后一个 UTF-8 字符。
            saved_text = raw[:capacity].decode("utf-8", errors="ignore")
            saved = saved_text.encode("utf-8")
            if raw and not saved:
                raise StorageError("附件剩余空间不足以保存一个完整字符，未保存。")
            reference = ArtifactRef(
                identity,
                source,
                hashlib.sha256(saved).hexdigest(),
                len(saved),
                len(saved_text),
                len(raw) if source_complete else None,
                source_complete and len(saved) == len(raw),
            )
            encoded = json.dumps(
                {"format_version": 1, "reference": asdict(reference), "text": saved_text},
                ensure_ascii=False,
            ).encode("utf-8")
            if len(encoded) > self._encoded_limit:
                raise StorageError("附件编码超过存储上限，未保存。")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._publish(path, encoded)
            return reference

    def describe(self, session_id: str, artifact_id: str) -> ArtifactRef:
        """读取并校验引用；缺失、坏编码、来源不符或内容版本损坏均抛出 StorageError。"""
        with self._locked(session_id, artifact_id) as path:
            reference, _text = self._read(path, session_id, artifact_id)
            return reference

    def read(
        self,
        session_id: str,
        artifact_id: str,
        *,
        expected_version: str,
        offset: int = 0,
        count: int | None = None,
    ) -> ArtifactPage:
        """按 Unicode 字符分页；expected_version 防止用旧引用读取被替换的内容。

        offset 从零开始，可等于已保存字符数；count 缺省为单页上限。非法范围抛出
        ValueError；文件或版本故障抛出 StorageError。每次有界验证完整附件，再返回一页。
        """
        limit = self.max_read_chars if count is None else count
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid artifact offset")
        if type(limit) is not int or not 1 <= limit <= self.max_read_chars:
            raise ValueError("invalid artifact page size")
        with self._locked(session_id, artifact_id) as path:
            reference, text = self._read(path, session_id, artifact_id)
            if expected_version != reference.version:
                raise StorageError("附件内容版本与引用不一致，拒绝读取。")
            if offset > reference.saved_chars:
                raise ValueError("artifact offset is beyond saved text")
            end = min(offset + limit, reference.saved_chars)
            return ArtifactPage(
                reference,
                offset,
                text[offset:end],
                end if end < reference.saved_chars else None,
            )

    @contextmanager
    def _locked(self, session_id: str, artifact_id: str) -> Iterator[Path]:
        """所有实例共用会话锁；校验身份和真实路径后才能访问附件，不能跨会话找同名文件。"""
        validate_artifact_identity(session_id, artifact_id)
        try:
            self._contained(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self._contained(self.root / ".locks" / f"{session_id}.lock")
            lock_path.parent.mkdir(exist_ok=True)
            with FileLock(lock_path, timeout=0):
                yield self._contained(self.root / session_id / f"{artifact_id}.json")
        except Timeout:
            raise StorageError("其他操作正在访问本会话附件，请稍后重试。") from None
        except OSError as error:
            raise StorageError("附件文件操作失败，请检查路径、权限和磁盘空间。") from error

    def _contained(self, path: Path) -> Path:
        """拒绝目录或文件链接重定向，包括指向另一个会话的链接。"""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as error:
            raise StorageError("附件路径无法解析，拒绝访问。") from error
        if resolved != path or not resolved.is_relative_to(self.root):
            raise StorageError("附件路径发生重定向或超出所属目录，拒绝访问。")
        return path

    def _usage(self, directory: Path, session_id: str) -> int:
        """持锁核对实际大小；达到配额立即停止扫描，坏记录不能被跳过以腾出额度。"""
        if not directory.exists():
            return 0
        used, items = 0, 0
        for visited, entry in enumerate(directory.iterdir(), 1):
            if visited > self.max_items * 2 + 32:
                raise StorageError("附件目录项过多，请先检查遗留文件。")
            if entry.suffix != ".json":
                continue
            items += 1
            if items >= self.max_items:
                raise StorageError("会话附件数量已达上限，未保存新附件。")
            reference, _text = self._read(self._contained(entry), session_id, entry.stem)
            used += reference.saved_bytes
            if used >= self.max_session_bytes:
                raise StorageError("会话附件字节配额已耗尽，未保存新附件。")
        return used

    def _read(self, path: Path, session_id: str, artifact_id: str) -> tuple[ArtifactRef, str]:
        """读取上限内的完整数据并验证格式、身份和正文；磁盘字段不能决定分配大小。"""
        try:
            with path.open("rb") as file:
                raw = file.read(self._encoded_limit + 1)
            if len(raw) > self._encoded_limit:
                raise ValueError("oversized artifact")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
            if not isinstance(payload, dict) or payload.keys() != {
                "format_version",
                "reference",
                "text",
            }:
                raise ValueError("invalid artifact fields")
            if type(payload["format_version"]) is not int or payload["format_version"] != 1:
                raise ValueError("unsupported artifact format")
            if not isinstance(payload["reference"], dict) or not isinstance(payload["text"], str):
                raise ValueError("invalid artifact values")
            metadata = dict(payload["reference"])
            source = ArtifactSource(**metadata.pop("source"))
            reference = ArtifactRef(source=source, **metadata)
            text = payload["text"]
            data = text.encode("utf-8")
            if (
                reference.source.session_id != session_id
                or reference.artifact_id != artifact_id
                or len(data) > self.max_bytes
                or reference.saved_bytes != len(data)
                or reference.saved_chars != len(text)
                or reference.version != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("artifact identity or content mismatch")
            return reference, text
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise StorageError("附件内容损坏、来源不符或格式不受支持，拒绝读取。") from error

    def _publish(self, path: Path, encoded: bytes) -> None:
        """同目录写入、flush/fsync 后原子发布；失败只清理本次临时文件。"""
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".artifact-", suffix=".tmp", dir=path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 临时文件不是可用引用；清理失败不能掩盖原始存储错误。
                    pass


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复字段，避免同一文件被解释成不同的来源或内容范围。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate artifact field")
        result[key] = value
    return result

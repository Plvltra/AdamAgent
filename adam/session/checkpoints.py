"""有界、不可变的编辑快照存储；与 JSONL 成功事实分离，不打开或恢复目标文件。"""

import base64
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.contracts.checkpoints import EditCheckpoint, validate_checkpoint_ids
from adam.contracts.errors import StorageError


class CheckpointStore:
    """每份原始内容独立原子发布；读写共用会话级锁，已有内容不能被不同快照覆盖。"""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        """max_bytes 是调用方注入的原文件字节上限，不在存储层另建工具配置。"""
        if type(max_bytes) is not int or not 0 < max_bytes <= 100_000_000:
            raise ValueError("invalid checkpoint byte limit")
        self.root = root.absolute()
        self.max_bytes = max_bytes
        self._encoded_limit = 4 * ((max_bytes + 2) // 3) + 65536

    def put(self, checkpoint: EditCheckpoint) -> None:
        """可靠保存修改前副本；相同 ID 与内容可重复保存，不同内容抛出 StorageError。

        超限、磁盘和锁故障均阻止调用方继续编辑。临时文件不会作为可用快照，
        发布前失败保留旧文件；本方法不写 JSONL，也不宣称编辑已完成。
        """
        if checkpoint.original is not None and len(checkpoint.original) > self.max_bytes:
            raise StorageError("编辑检查点超过文件字节上限，未保存。")
        payload = asdict(checkpoint)
        payload["path"] = str(checkpoint.path)
        version = 2 if checkpoint.operation == "create" else 1
        if checkpoint.original is not None:
            payload["original"] = base64.b64encode(checkpoint.original).decode("ascii")
        # 既有替换快照保持v1字节契约；创建使用v2明确操作和null，不能伪造为空文件副本。
        if version == 1:
            del payload["operation"]
        encoded = json.dumps({"version": version, **payload}, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._encoded_limit:
            raise StorageError("编辑检查点元数据过大，未保存。")
        with self._locked(checkpoint.session_id, checkpoint.edit_id) as path:
            if path.exists():
                if self._read(path, checkpoint.session_id, checkpoint.edit_id) != checkpoint:
                    raise StorageError("同一编辑 ID 已有不同检查点，拒绝覆盖。")
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            self._publish(path, encoded)

    def load(self, session_id: str, edit_id: str) -> EditCheckpoint:
        """校验原字节或缺失标记与身份；缺失/损坏抛StorageError，不跳过或推断成功。"""
        with self._locked(session_id, edit_id) as path:
            return self._read(path, session_id, edit_id)

    @contextmanager
    def _locked(self, session_id: str, edit_id: str) -> Iterator[Path]:
        """协调独立实例的读写，并在访问前拒绝目录链接越界；不是操作系统沙箱。"""
        validate_checkpoint_ids(session_id, edit_id)
        try:
            if self.root.resolve() != self.root:
                raise StorageError("检查点根目录不能通过链接重定向。")
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self._contained(self.root / ".locks" / f"{session_id}.lock")
            lock_path.parent.mkdir(exist_ok=True)
            with FileLock(lock_path, timeout=0):
                yield self._contained(self.root / session_id / f"{edit_id}.json")
        except Timeout:
            raise StorageError("其他操作正在访问本会话检查点，请稍后重试。") from None
        except OSError as error:
            raise StorageError("检查点文件操作失败，请检查路径、权限和磁盘空间。") from error

    def _contained(self, path: Path) -> Path:
        """校验目录和文件链接后的真实位置；身份检查在拼接路径前完成。"""
        if not path.resolve().is_relative_to(self.root):
            raise StorageError("检查点路径超出存储目录。")
        return path

    def _read(self, path: Path, session_id: str, edit_id: str) -> EditCheckpoint:
        """限量读取后验证格式、哈希和来源；目标路径只作为数据，不做文件访问。"""
        try:
            with path.open("rb") as file:
                encoded = file.read(self._encoded_limit + 1)
            if len(encoded) > self._encoded_limit:
                raise ValueError("oversized checkpoint")
            payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_fields)
            if not isinstance(payload, dict):
                raise ValueError("invalid checkpoint object")
            version = payload.get("version")
            if type(version) is not int or version not in (1, 2):
                raise ValueError("unsupported checkpoint version")
            keys = {field.name for field in fields(EditCheckpoint)} | {"version"}
            if version == 1:
                keys -= {"operation"}
            if payload.keys() != keys:
                raise ValueError("invalid checkpoint fields")
            del payload["version"]
            common = payload.keys() - {"original", "before_version"}
            if any(not isinstance(payload[key], str) for key in common):
                raise ValueError("invalid checkpoint values")
            if version == 1:
                if not isinstance(payload["original"], str):
                    raise ValueError("invalid original bytes")
                payload["original"] = base64.b64decode(payload["original"], validate=True)
                if len(payload["original"]) > self.max_bytes:
                    raise ValueError("oversized original")
            elif (
                payload["operation"] != "create"
                or payload["original"] is not None
                or payload["before_version"] is not None
            ):
                raise ValueError("invalid create checkpoint")
            payload["path"] = Path(payload["path"])
            checkpoint = EditCheckpoint(**payload)
            if checkpoint.session_id != session_id or checkpoint.edit_id != edit_id:
                raise ValueError("mismatched checkpoint identity")
            return checkpoint
        except (ValueError, TypeError, RecursionError) as error:
            raise StorageError("检查点内容损坏或格式不受支持，拒绝读取。") from error

    def _publish(self, path: Path, encoded: bytes) -> None:
        """同目录写入、flush/fsync 后原子发布；只清理本次创建的临时文件。"""
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".checkpoint-", suffix=".tmp", dir=path.parent, delete=False
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
                    # 清理故障不掩盖原错误；孤立临时文件不会被 load 当作已发布快照。
                    pass


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复字段，避免相同原始记录被不同读取器解释成不同来源。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate checkpoint field")
        result[key] = value
    return result

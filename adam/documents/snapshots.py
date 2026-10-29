"""文档和向量缓存共用的有界 JSON 快照与原子保存。"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from adam.documents.models import DocumentError

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def digest(value: object) -> str:
    """使用稳定 JSON 编码计算缓存内容摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_snapshot(path: Path) -> dict[str, Any] | None:
    """验证容器版本及校验和；缺失返回 None，损坏保留原文件并报错。"""
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("oversized snapshot")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.keys() != {"version", "payload", "checksum"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["payload"], dict)
            or value["checksum"] != digest(value["payload"])
        ):
            raise ValueError("snapshot schema or checksum")
        return value["payload"]
    except (ValueError, TypeError, KeyError) as error:
        raise DocumentError("文档缓存损坏或版本不支持；保留原文件，请移走该缓存后重建。") from error


def save_snapshot(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件并 fsync，再原子替换；失败时移除临时文件。"""
    raw = json.dumps(
        {"version": 1, "payload": payload, "checksum": digest(payload)},
        ensure_ascii=False,
    ).encode("utf-8")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise DocumentError("文档缓存超出 64 MiB，请缩小语料。")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass

"""读取同一份文件字节并生成版本标识；编辑规划不依赖展示时的截断文本。"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.tool_calls.file_evidence import FileStateError


@dataclass(frozen=True)
class TextSnapshot:
    """一次完整读取的原字节与 UTF-8 文本；BOM 仍保留在 raw 中。"""

    path: Path
    raw: bytes
    text: str

    @property
    def version(self) -> str:
        return sha256(self.raw).hexdigest()


def read_snapshot(
    path: Path, max_bytes: int, cancel: CancellationToken | None = None
) -> TextSnapshot:
    """有界读取普通 UTF-8 文件；版本与文本来自同一次读取，不再次打开文件计算摘要。

    完整字节已取得但解码/文本类型失败时，FileStateError 携带版本供重复失败检测。
    未读完整或 IO 失败不猜版本，也不放宽原读取上限。
    """
    if cancel:
        cancel.check()
    if not path.is_file():
        raise ToolError("不是普通文件。")
    parts = []
    remaining = max_bytes + 1
    with path.open("rb") as stream:
        while remaining > 0:
            if cancel:
                cancel.check()
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
    if cancel:
        cancel.check()
    raw = b"".join(parts)
    if len(raw) > max_bytes:
        raise ToolError("文件超过文本大小上限。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise FileStateError("只支持 UTF-8 文本文件。", version=sha256(raw).hexdigest()) from error
    if "\0" in text:
        raise FileStateError("不支持二进制文件。", version=sha256(raw).hexdigest())
    return TextSnapshot(path, raw, text)

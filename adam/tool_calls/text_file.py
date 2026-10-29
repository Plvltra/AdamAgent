"""读取同一份文件字节并生成版本标识；编辑规划不依赖展示时的截断文本。"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from adam.contracts.errors import ToolError


@dataclass(frozen=True)
class TextSnapshot:
    """一次完整读取的原字节与 UTF-8 文本；BOM 仍保留在 raw 中。"""

    path: Path
    raw: bytes
    text: str

    @property
    def version(self) -> str:
        return sha256(self.raw).hexdigest()


def read_snapshot(path: Path, max_bytes: int) -> TextSnapshot:
    """有界读取普通 UTF-8 文件；版本与文本来自同一次读取，不再次打开文件计算摘要。"""
    if not path.is_file():
        raise ToolError("不是普通文件。")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ToolError("文件超过文本大小上限。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise ToolError("只支持 UTF-8 文本文件。") from error
    if "\0" in text:
        raise ToolError("不支持二进制文件。")
    return TextSnapshot(path, raw, text)

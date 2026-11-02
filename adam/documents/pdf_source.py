"""授权后读取 PDF 原字节，并在使用结果前复查路径与读取快照。"""

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adam.documents.models import DocumentError
from adam.documents.pdf import PdfLimits


@dataclass(frozen=True)
class PdfSnapshot:
    """原字节与来源版本；stamp 用于发现读取/解析期间常见文件替换，不代表文件锁。"""

    path: Path
    data: bytes
    version: str
    stamp: tuple[int, int, int, int]

    def verify(self, authorize: Callable[[str], Path]) -> None:
        """重新授权并核对当前文件元数据；撤权或外部修改后不能交付旧读取结果。"""
        if authorize(str(self.path)) != self.path or _stamp(self.path.stat()) != self.stamp:
            raise DocumentError("PDF 路径或内容在读取时变化，请重新查询。")


def read_pdf_snapshot(
    path: Path,
    authorize: Callable[[str], Path],
    limits: PdfLimits,
    check_cancel: Callable[[], None],
) -> PdfSnapshot:
    """先授权再有界读取，比较同一句柄前后及当前路径；不解码、不缓存原文件。

    元数据检查不能锁住外部写入，但版本始终来自本次实际读取的完整字节。
    文件错误交给调用层统一提示；取消原样传播。
    """
    check_cancel()
    actual = authorize(str(path))
    if actual.suffix.lower() != ".pdf" or not actual.is_file():
        raise DocumentError("read_pdf 只接受已有的普通 PDF 文件。")
    with actual.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limits.max_file_bytes:
            raise DocumentError("PDF 不是普通文件或原文件字节数超限。")
        check_cancel()
        data = stream.read(limits.max_file_bytes + 1)
        after = os.fstat(stream.fileno())
    check_cancel()
    if len(data) > limits.max_file_bytes or _stamp(before) != _stamp(after):
        raise DocumentError("PDF 在读取时变化或原文件字节数超限。")
    snapshot = PdfSnapshot(actual, data, hashlib.sha256(data).hexdigest(), _stamp(after))
    snapshot.verify(authorize)
    return snapshot


def _stamp(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

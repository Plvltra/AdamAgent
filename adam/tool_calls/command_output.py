"""持续排空命令管道，以固定字节容量保留开头和最近尾部。"""

import codecs
from dataclasses import dataclass
from threading import Lock
from typing import BinaryIO


@dataclass(frozen=True)
class CapturedOutput:
    """一次不可变读取快照；read_bytes 是已读数量，eof=false 时不是整条输出的总量。"""

    text: str
    read_bytes: int
    omitted_bytes: int
    eof: bool
    invalid_utf8: bool

    @property
    def truncated(self) -> bool:
        """丢弃字节、尚未读完或替换了非法编码，都不能宣称文本完整。"""
        return bool(self.omitted_bytes or not self.eof or self.invalid_utf8)

    def metadata(self) -> dict[str, int | bool]:
        """分别记录源头字节省略与读取状态；标记文字本身不计入原输出字节。"""
        return {
            "read_bytes": self.read_bytes,
            "omitted_bytes": self.omitted_bytes,
            "eof": self.eof,
            "invalid_utf8": self.invalid_utf8,
        }


class OutputCapture:
    """持续读取到 EOF，保留的头尾原字节不超过 limit；单写线程可与快照读取并行。"""

    def __init__(self, limit: int) -> None:
        """前半容量固定为开头，后半滚动保留尾部；小于容量的内容全部保留。"""
        if type(limit) is not int or limit < 1:
            raise ValueError("command capture limit must be positive")
        self._head_limit, self._tail_limit = (limit + 1) // 2, limit // 2
        self._head, self._tail = bytearray(), bytearray()
        self._read_bytes = 0
        self._eof = False
        self._lock = Lock()

    def drain(self, stream: BinaryIO) -> None:
        """排空已启动进程的管道；读取失败保留已有证据，eof 仍为 false。"""
        try:
            with stream:
                while chunk := stream.read(4096):
                    self._append(chunk)
                with self._lock:
                    self._eof = True
        except OSError:
            # 不能读取并不代表剩余内容为空；快照同时保留已读字节和未知结尾。
            pass

    def snapshot(self) -> CapturedOutput:
        """取得一致头尾和计数；UTF-8 分界残片计入省略，保留段中的非法编码另行标记。"""
        with self._lock:
            head, tail = bytes(self._head), bytes(self._tail)
            total, eof = self._read_bytes, self._eof
        omitted = total - len(head) - len(tail)
        if not omitted:
            text, invalid = _decode(head + tail)
            return CapturedOutput(text, total, 0, eof, invalid)
        # 头部末字和尾部首字可能被容量切开；不把人为切口显示成损坏字符。
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        prefix = decoder.decode(head, final=False)
        pending = len(decoder.getstate()[0])
        boundary = 0
        while boundary < min(3, len(tail)) and 0x80 <= tail[boundary] <= 0xBF:
            boundary += 1
        suffix, tail_invalid = _decode(tail[boundary:])
        _, head_invalid = _decode(head[:-pending] if pending else head)
        omitted += pending + boundary
        text = f"{prefix}\n[...省略 {omitted} 字节...]\n{suffix}"
        return CapturedOutput(text, total, omitted, eof, head_invalid or tail_invalid)

    def _append(self, chunk: bytes) -> None:
        """每批只保存尚缺的头部和最近尾部；计数包含全部已读字节。"""
        with self._lock:
            self._read_bytes += len(chunk)
            head_count = min(len(chunk), self._head_limit - len(self._head))
            self._head.extend(chunk[:head_count])
            remaining = chunk[head_count:]
            if self._tail_limit:
                self._tail.extend(remaining[-self._tail_limit :])
                if len(self._tail) > self._tail_limit:
                    del self._tail[: -self._tail_limit]


def _decode(raw: bytes) -> tuple[str, bool]:
    """按 UTF-8 解码；替换非法字节时明确标记，不宣称保留原始输出。"""
    try:
        return raw.decode("utf-8"), False
    except UnicodeError:
        return raw.decode("utf-8", errors="replace"), True

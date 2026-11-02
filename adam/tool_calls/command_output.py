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

    def drain(self, stream: BinaryIO, live: bool = False) -> None:
        """排空已启动进程的管道；live 启用及时读取；失败保留证据，eof 仍为 false。"""
        try:
            with stream:
                # read1 返回当前可读数据，避免短输出一直等到缓冲区满或进程退出。
                read = getattr(stream, "read1", stream.read) if live else stream.read
                while chunk := read(4096):
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

    def read_since(self, cursor: int = 0, max_bytes: int = 4096) -> dict[str, str | int | bool]:
        """按字节游标续读一个保留段；丢失量明确返回，末尾半个 UTF-8 字符留待下次。

        cursor 使用上次 next_cursor，属于当前捕获流。头尾间的缺口在跨越时报告；
        eof 只表示管道结束，has_more 表示本次快照还有尚未消费的字节。
        """
        if type(cursor) is not int or cursor < 0:
            raise ValueError("output cursor must be a nonnegative integer")
        if type(max_bytes) is not int or not 4 <= max_bytes <= 65536:
            raise ValueError("output page size must be between 4 and 65536 bytes")
        with self._lock:
            head, tail = bytes(self._head), bytes(self._tail)
            total, eof = self._read_bytes, self._eof
        if cursor > total:
            raise ValueError("output cursor exceeds captured bytes")
        tail_start = total - len(tail)
        if tail_start == len(head):
            start, available = cursor, (head + tail)[cursor:]
        elif cursor < len(head):
            start, available = cursor, head[cursor:]
        else:
            start = max(cursor, tail_start)
            available = tail[start - tail_start :]
        # 缺口后的尾段可能从字符中间开始；该残片也算丢失字节。
        if start > cursor:
            while available and 0x80 <= available[0] <= 0xBF:
                start += 1
                available = available[1:]
        raw = available[:max_bytes]
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=eof and start + len(raw) == total)
        consumed = len(raw) - len(decoder.getstate()[0])
        # 保留头部后面是永久缺口时，半个字符无法补全，跳过并报告。
        lost_boundary = 0
        if start + len(raw) == len(head) < tail_start:
            lost_boundary = len(raw) - consumed
            consumed = len(raw)
        next_cursor = start + consumed
        return {
            "text": text,
            "next_cursor": next_cursor,
            "dropped_bytes": start - cursor + lost_boundary,
            "has_more": next_cursor < total,
            "eof": eof,
            "invalid_utf8": _decode(raw[: consumed - lost_boundary])[1],
        }

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

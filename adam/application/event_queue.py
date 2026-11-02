"""线程安全的运行内事件邮箱；只存数据，消费与持久记录由应用安全点协调。"""

import hashlib
import json
from collections import deque
from itertools import islice
from threading import Lock

from adam.contracts.events import EventKind, EventReceipt, InputEvent


class EventQueue:
    """绑定一次运行的有界FIFO；满时显式拒绝，关闭后可读完已有事件。

    peek不移除数据，消费者保存处理结果后再ack队首；只有一个协调消费者。
    去重索引保留整个运行，达到max_events后拒绝新事件，避免无限缓存或旧ID再次生效。
    队列不提供跨进程持久性，重启不能据内存回执声称已处理或自动重发。
    """

    def __init__(
        self, task_id: str, *, capacity: int = 64, max_events: int = 1024, max_chars: int = 8192
    ) -> None:
        self._identity(task_id)
        if (
            any(type(value) is not int for value in (capacity, max_events, max_chars))
            or not 1 <= capacity <= max_events <= 10000
            or not 1 <= max_chars <= 65536
        ):
            raise ValueError("invalid event queue limits")
        self.task_id = task_id
        self.capacity, self.max_events, self.max_chars = capacity, max_events, max_chars
        self._pending: deque[InputEvent] = deque()
        self._seen: dict[tuple[str, str], tuple[int, str]] = {}
        self._closed = False
        self._lock = Lock()

    def put(self, kind: EventKind, source: str, event_id: str, text: str) -> EventReceipt:
        """原子分配递增序号；同ID同内容返回原回执，内容冲突或容量不足抛ValueError。

        序号表示成功入队顺序，不推断多个发送线程的业务先后；重试不改变顺序。
        来源/类型由宿主入口赋值，队列本身不做身份认证。
        """
        self._identity(source)
        self._identity(event_id)
        if (
            not isinstance(kind, EventKind)
            or not isinstance(text, str)
            or not text
            or len(text) > self.max_chars
        ):
            raise ValueError("invalid event kind or text size")
        fingerprint = hashlib.sha256(
            json.dumps([kind.value, text], ensure_ascii=True).encode()
        ).hexdigest()
        key = (source, event_id)
        with self._lock:
            if self._closed:
                raise ValueError("event queue is closed")
            if key in self._seen:
                sequence, previous = self._seen[key]
                if previous != fingerprint:
                    raise ValueError("event ID reused with different content")
                return EventReceipt(sequence, True)
            if len(self._pending) >= self.capacity or len(self._seen) >= self.max_events:
                raise ValueError("event queue capacity exhausted")
            sequence = len(self._seen) + 1
            self._pending.append(InputEvent(self.task_id, sequence, event_id, kind, source, text))
            self._seen[key] = (sequence, fingerprint)
            return EventReceipt(sequence, False)

    def peek(self, limit: int = 8) -> tuple[InputEvent, ...]:
        """取得最多limit个队首事件的不可变快照；读取本身不代表处理成功。"""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid event batch size")
        with self._lock:
            return tuple(islice(self._pending, limit))

    def ack(self, sequence: int) -> None:
        """仅确认当前队首，拒绝跳过事件；确认后同ID重试仍不会重新入队。"""
        with self._lock:
            if (
                type(sequence) is not int
                or not self._pending
                or self._pending[0].sequence != sequence
            ):
                raise ValueError("only the first pending event can be acknowledged")
            self._pending.popleft()

    def close(self) -> None:
        """拒绝后续所有投递；不丢弃未处理事件，允许peek/ack，重复关闭无副作用。"""
        with self._lock:
            self._closed = True

    def close_if_empty(self) -> bool:
        """原子封口用于最终回答边界；有未处理输入时返回False并继续接收。"""
        with self._lock:
            if self._pending:
                return False
            self._closed = True
            return True

    @staticmethod
    def _identity(value: str) -> None:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or not value.isascii()
            or any(not (char.isalnum() or char in "_.:-") for char in value)
        ):
            raise ValueError("event identity must be 1-128 ASCII identifier characters")

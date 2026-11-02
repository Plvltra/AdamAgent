"""B27.1：运行内补充事件的顺序、容量、去重和关闭契约。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from adam.application.event_queue import EventQueue
from adam.contracts.events import EventKind


def test_duplicate_retry_conflict_and_ack_preserve_order():
    queue = EventQueue("run-1", capacity=2)
    first = queue.put(EventKind.USER_INPUT, "terminal", "one", "补充要求")
    assert queue.put(EventKind.USER_INPUT, "terminal", "one", "补充要求").duplicate
    with pytest.raises(ValueError, match="different content"):
        queue.put(EventKind.USER_INPUT, "terminal", "one", "修改过的内容")
    second = queue.put(EventKind.TASK_UPDATE, "worker", "two", "ready")
    assert [e.sequence for e in queue.peek(2)] == [first.sequence, second.sequence]
    with pytest.raises(ValueError, match="first pending"):
        queue.ack(second.sequence)
    queue.ack(first.sequence)
    assert queue.put(EventKind.USER_INPUT, "terminal", "one", "补充要求").duplicate
    assert [e.sequence for e in queue.peek(2)] == [second.sequence]


def test_capacity_text_limit_lifetime_limit_and_close_are_explicit():
    queue = EventQueue("run-1", capacity=1, max_events=2, max_chars=4)
    queue.put(EventKind.USER_INPUT, "ui", "1", "text")
    with pytest.raises(ValueError, match="capacity"):
        queue.put(EventKind.USER_INPUT, "ui", "2", "text")
    assert queue.peek()[0].sequence == 1
    queue.ack(1)
    with pytest.raises(ValueError, match="text size"):
        queue.put(EventKind.USER_INPUT, "ui", "2", "too long")
    queue.put(EventKind.USER_INPUT, "ui", "2", "next")
    queue.ack(2)
    with pytest.raises(ValueError, match="capacity"):
        queue.put(EventKind.USER_INPUT, "ui", "3", "text")
    other = EventQueue("run-2")
    other.put(EventKind.USER_INPUT, "ui", "1", "text")
    other.close()
    other.close()
    with pytest.raises(ValueError, match="closed"):
        other.put(EventKind.USER_INPUT, "ui", "1", "text")
    assert other.peek()[0].task_id == "run-2"
    other.ack(1)
    assert other.peek() == ()


def test_concurrent_retries_assign_one_sequence_per_unique_event():
    queue = EventQueue("run-parallel", capacity=32)

    def send(index):
        return queue.put(EventKind.AGENT_MESSAGE, "child", str(index % 16), str(index % 16))

    with ThreadPoolExecutor(max_workers=4) as workers:
        receipts = list(workers.map(send, range(64)))
    assert sum(not receipt.duplicate for receipt in receipts) == 16
    events = queue.peek(32)
    assert [event.sequence for event in events] == list(range(1, 17))
    assert len({event.event_id for event in events}) == 16
    assert {receipt.sequence for receipt in receipts} == set(range(1, 17))

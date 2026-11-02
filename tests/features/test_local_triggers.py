"""B29.2：条件合并、队列背压与跨运行隔离。"""

from adam.application.event_queue import EventQueue
from adam.application.triggers import LocalTriggers
from adam.contracts.events import EventKind


def test_conditions_merge_and_keep_original_run(tmp_path):
    snapshot = {"status": "running", "stdout": {"text": ""}, "stderr": {"text": ""}}
    triggers = LocalTriggers(tmp_path, lambda *args: snapshot)
    try:
        owner = "1234abcd"
        queue = EventQueue("turn")
        first = triggers.watch(owner, "turn", "job", "output")
        assert triggers.watch(owner, "turn", "job", "output") == first
        triggers.watch(owner, "turn", "job", "completed")
        triggers.schedule(owner, "turn", 10, "timer")
        assert triggers.poll(owner, queue, 9) == 0
        snapshot["stdout"]["text"] = "many lines"
        assert triggers.poll(owner, queue, 10) == 2
        assert {e.kind for e in queue.peek()} == {EventKind.TASK_UPDATE, EventKind.TIMER}
        assert triggers.poll(owner, queue, 11) == 0
        snapshot["status"] = "completed"
        assert triggers.poll(owner, queue, 12) == 1
        assert len(queue.peek()) == 3
        triggers.schedule(owner, "old-turn", 999, "never next turn")
        assert triggers.poll(owner, EventQueue("new-turn"), 13) == 0
        assert triggers.records(owner)[-1].status == "expired"
    finally:
        triggers.close()


def test_full_queue_keeps_timer_pending_until_acceptance(tmp_path):
    triggers = LocalTriggers(tmp_path, lambda *args: {})
    try:
        owner = "1234abcd"
        queue = EventQueue("turn", capacity=1)
        queue.put(EventKind.USER_INPUT, "terminal", "user", "question")
        timer = triggers.schedule(owner, "turn", 0, "reminder")
        assert triggers.poll(owner, queue, 0) == 0
        assert triggers.records(owner)[0].status == "pending"
        queue.ack(1)
        assert triggers.poll(owner, queue, 0) == 1
        assert queue.peek()[0].event_id == timer["timer_id"]
        assert triggers.poll(owner, queue, 0) == 0
    finally:
        triggers.close()

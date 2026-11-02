"""B29.1：定时查询边界、取消与持久恢复；读取绝不隐式执行任务。"""

import pytest

from adam.application.timers import LocalTimers
from adam.contracts.errors import StorageError


def test_timer_due_cancel_and_reopen(tmp_path):
    timers = LocalTimers(tmp_path)
    try:
        first = timers.schedule("turn-a", 100, "检查任务")
        second = timers.schedule("turn-b", 50, "取消此提醒")
        timers.cancel(second.timer_id)
        assert timers.due(99) == ()
        assert timers.due(100) == (first,)
        assert timers.due(101) == (first,)
        with pytest.raises(StorageError):
            LocalTimers(tmp_path)
    finally:
        timers.close()
    restored = LocalTimers(tmp_path)
    try:
        assert restored.due(200) == (first,)
        restored.finish(first.timer_id, "delivered")
        assert restored.due(200) == ()
        assert [item.status for item in restored.list()] == ["delivered", "cancelled"]
    finally:
        restored.close()


def test_timer_failed_save_keeps_old_state(tmp_path, monkeypatch):
    timers = LocalTimers(tmp_path)
    try:
        item = timers.schedule("turn", 0, "reminder")

        def fail(*args):
            raise OSError("injected replace failure")

        monkeypatch.setattr("adam.application.timers.os.replace", fail)
        with pytest.raises(StorageError):
            timers.cancel(item.timer_id)
        assert timers.due(0) == (item,)
    finally:
        timers.close()

"""B29.3：事件已持久接受、提醒尚未结算时等待父进程强杀。"""

import sys
import time
from pathlib import Path

from adam.application.running_inputs import RunningInputs
from adam.application.timers import LocalTimers
from adam.application.triggers import LocalTriggers
from adam.contracts.cancellation import CancellationToken


def run(root):
    """暂停位于两个真实持久记录之间；重启不得重发已接受事件。"""
    inputs = RunningInputs(root / "inputs", CancellationToken())
    inputs.begin("1234abcd", "turn")
    triggers = LocalTriggers(root / "triggers", lambda *args: {})
    triggers.schedule("1234abcd", "turn", 0, "preserve once")
    original = LocalTimers.finish

    def finish(self, timer_id, status):
        if status == "delivered":
            (root / "ready").touch()
            while True:
                time.sleep(0.1)
        return original(self, timer_id, status)

    LocalTimers.finish = finish
    triggers.poll("1234abcd", inputs.queue("turn"), time.time())


if __name__ == "__main__":
    run(Path(sys.argv[1]))

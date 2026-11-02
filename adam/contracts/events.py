"""运行中补充事件的数据契约；来源标签不授予执行权限。"""

from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    """由可信入口选择事件类型，不能把任意工具文本提升为用户指令。"""

    USER_INPUT = "user_input"
    AGENT_MESSAGE = "agent_message"
    TASK_UPDATE = "task_update"
    TIMER = "timer"


@dataclass(frozen=True)
class InputEvent:
    """不可变事件；task_id标识一次运行，sequence由该运行的队列分配。"""

    task_id: str
    sequence: int
    event_id: str
    kind: EventKind
    source: str
    text: str


@dataclass(frozen=True)
class EventReceipt:
    """入队回执；duplicate表示同一来源与事件ID的相同内容已被接受。"""

    sequence: int
    duplicate: bool

"""存储与上下文共用的成功轮次数据；不依赖文件或模型服务。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HistoryTurn:
    """已完整提交的协议消息；turn_id 用于追溯上下文来源。"""

    turn_id: str
    messages: list[dict[str, Any]]

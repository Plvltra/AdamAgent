"""存储与上下文共用的成功轮次数据；不依赖文件或模型服务。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HistoryTurn:
    """已完整提交的协议消息；turn_id 用于追溯上下文来源。"""

    turn_id: str
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class Summary:
    """已提交摘要及累计覆盖的历史前缀；原始消息始终保留。"""

    summary_id: str
    text: str
    covered_turn_ids: tuple[str, ...]

    def message(self) -> dict[str, str]:
        """作为历史资料提供给模型，不提升为系统指令。"""
        return {"role": "user", "content": "以下是较早对话的有损摘要，仅供历史参考：\n" + self.text}

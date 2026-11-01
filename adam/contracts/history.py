"""存储与上下文共用的成功轮次数据；不依赖文件或模型服务。"""

from dataclasses import dataclass
from typing import Any

CONTEXT_LAYOUT_VERSION = "stable_prefix_v1"
COMPACTION_VERSION = "task_aware_v1"


def validate_compaction_version(value: object) -> None:
    """None 保留旧摘要输入；未知版本拒绝重放，不猜测当时的压缩策略。"""
    if value is not None and value != COMPACTION_VERSION:
        raise ValueError("unknown compaction version")


def validate_context_layout(value: object) -> None:
    """None 表示旧消息排列；未知布局拒绝读取，不以当前顺序猜测旧请求。"""
    if value is not None and value != CONTEXT_LAYOUT_VERSION:
        raise ValueError("unknown context layout")


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

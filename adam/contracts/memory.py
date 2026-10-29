"""跨会话记忆的不可变记录；版本来源独立于聊天消息。"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class MemorySource:
    """用户明确执行的操作及可选来源会话；无会话时不伪造关联。"""

    operation: str
    session_id: str | None = None
    actor: str = "user"


@dataclass(frozen=True)
class MemoryRecord:
    """一条记忆的一个版本；project 为 None 表示用户显式选择的全局范围。"""

    memory_id: str
    revision: int
    content: str
    project: str | None
    created_at: str
    updated_at: str
    source: MemorySource
    expires_at: str | None = None
    deleted: bool = False

    def active(self, now: datetime) -> bool:
        """判断指定时刻是否可检索；保留过期和删除版本供人工追溯。"""
        return not self.deleted and (self.expires_at is None or timestamp(self.expires_at) > now)

    def to_dict(self) -> dict[str, Any]:
        """返回独立的序列化快照，供存储和请求使用记录引用。"""
        return asdict(self)


@dataclass(frozen=True)
class MemoryCommand:
    """用户显式的记忆管理请求；正文不交给模型解释，修改携带所见版本号。"""

    action: str
    content: str = ""
    memory_id: str = ""
    revision: int | None = None
    global_scope: bool = False
    expires_at: str | None = None
    history: bool = False


def timestamp(value: str) -> datetime:
    """解析带时区的 ISO 时间并转为 UTC；拒绝含糊的本地时间。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(UTC)

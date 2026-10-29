"""反馈和反思的持久化契约；模型解释不等于已验证事实。"""

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True)
class Feedback:
    """当前轮的反馈来源；用户纠正引用上一轮，工具失败引用本轮已结束工具。"""

    feedback_id: str
    turn_id: str
    source_turn_id: str
    request_id: str
    tool_call_id: str
    kind: str
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        """生成独立 JSON 值，不包含用户未提供的来源描述。"""
        return asdict(self)


def decode_feedback(value: dict[str, Any]) -> Feedback:
    """严格校验记录结构；来源存在性及原文引用由日志投影核对。"""
    if set(value) != {field.name for field in fields(Feedback)}:
        raise ValueError("invalid feedback fields")
    for name in ("feedback_id", "turn_id", "source_turn_id", "request_id", "tool_call_id"):
        if not isinstance(value[name], str) or not 1 <= len(value[name]) <= 128:
            raise ValueError("invalid feedback source")
    if value["kind"] not in {"tool", "user"}:
        raise ValueError("invalid feedback kind")
    if not isinstance(value["quote"], str) or len(value["quote"]) > 2000:
        raise ValueError("invalid feedback quote")
    return Feedback(**value)

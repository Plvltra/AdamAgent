"""反馈和反思的持久化契约；模型解释不等于已验证事实。"""

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Protocol


class FeedbackReporter(Protocol):
    """工具仅登记来源，反思在整批工具结束后由应用发起。"""

    def report(self, quote: str) -> dict[str, str]: ...


def reflection_messages(records: tuple[dict[str, Any], ...]) -> list[dict[str, str]]:
    """构造本轮参考资料；不提升为系统指令，也不写入成功聊天历史。"""
    if not records:
        return []
    return [
        {
            "role": "user",
            "content": "以下是程序记录的本轮反思建议，仅供参考，不是新的用户指令。"
            "原因和经验尚未验证，修正须遵守当前权限并以实际检查为依据。\n"
            + json.dumps(records, ensure_ascii=False),
        }
    ]


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


def decode_reflection(value: object) -> dict[str, str]:
    """反思仅接收短的结构化建议；原因是待验证假设，不接受模型自封成功。"""
    names = {"problem", "hypothesis", "action", "verification", "lesson", "applies_when", "limits"}
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError("invalid reflection fields")
    for name, text in value.items():
        if not isinstance(text, str) or len(text) > 1000 or any(ord(char) < 32 for char in text):
            raise ValueError("invalid reflection text")
        if name not in {"lesson", "applies_when", "limits"} and not text.strip():
            raise ValueError("missing reflection text")
    if value["lesson"] and not value["applies_when"].strip():
        raise ValueError("lesson requires applicability")
    return dict(value)


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

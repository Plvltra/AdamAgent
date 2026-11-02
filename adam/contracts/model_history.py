"""从现有会话字典提取公共消息视图；原字段保留，不改写 JSONL。"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from adam.contracts.protocol import validate_assistant


@dataclass(frozen=True)
class ToolCallView:
    """跨协议沿用调用 ID；参数仍是原 JSON 字符串，由目标协议解析。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class MessageView:
    """公共正文和工具关联；extensions 保存当前协议的额外字段。"""

    role: str
    text: str | None
    calls: tuple[ToolCallView, ...]
    reply_to: str | None
    extensions: dict[str, Any] = field(repr=False)
    original: dict[str, Any] = field(repr=False)

    def to_raw(self) -> dict[str, Any]:
        """返回原消息的独立副本，包含推理等扩展字段，不共享可变字典。"""
        return deepcopy(self.original)


@dataclass(frozen=True)
class ProtocolCapabilities:
    """本版适配器支持的能力，不代表远端模型一定支持。"""

    name: str
    streaming: bool
    tools: bool = True
    reasoning_field: str | None = None


def protocol_capabilities(name: str) -> ProtocolCapabilities:
    """返回两种已计划支持的协议边界；未知名称抛 ValueError。"""
    if name == "chat_completions":
        return ProtocolCapabilities(name, streaming=True, reasoning_field="reasoning_content")
    if name == "messages":
        return ProtocolCapabilities(name, streaming=False)
    raise ValueError("不支持的模型协议。")


def message_view(raw: dict[str, Any]) -> MessageView:
    """校验一条文本/工具消息并保留全部原字段；不校验整轮配对或执行工具。

    不支持的角色、非文本正文和无效工具身份抛 ValueError。扩展字段只保存，
    目标适配器必须明确处理或拒绝，不能在切换协议时静默删除。
    """
    role = raw.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("不支持的消息角色。")
    if role == "assistant":
        validate_assistant(raw)
    elif not isinstance(raw.get("content"), str):
        raise ValueError("仅支持文本消息。")
    reply_to = raw.get("tool_call_id") if role == "tool" else None
    if role == "tool" and (not isinstance(reply_to, str) or not reply_to):
        raise ValueError("工具结果缺少调用 ID。")
    calls = (
        tuple(
            ToolCallView(call["id"], call["function"]["name"], call["function"]["arguments"])
            for call in (raw.get("tool_calls") or [])
        )
        if role == "assistant"
        else ()
    )
    common = {"role", "content"}
    common |= (
        {"tool_calls"} if role == "assistant" else {"tool_call_id"} if role == "tool" else set()
    )
    return MessageView(
        str(role),
        raw.get("content"),
        calls,
        reply_to,
        deepcopy({key: value for key, value in raw.items() if key not in common}),
        deepcopy(raw),
    )

"""文字、固定媒体引用与模型能力的公共视图；不包含 SDK、路径或媒体正文。"""

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from adam.contracts.errors import AgentError
from adam.contracts.media import MediaRef, decode_media_reference

MEDIA_INPUT_RESERVE = 8192


@dataclass(frozen=True)
class TextPart:
    """一段原始文字，不将媒体引用或文件名冒充图像内容。"""

    text: str


@dataclass(frozen=True)
class MediaPart:
    """有所有者和完整版本的媒体；字节由宿主的窄读取端口取得。"""

    reference: MediaRef


@dataclass(frozen=True)
class ToolCallIdentity:
    """跨包保留工具 ID、名称和原始参数，不重新生成 ID 或重排结果。"""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class MessageView:
    """现有消息的只读视图；文字在前，media 按输入顺序排列，不使用供应商图片 URL。"""

    role: str
    parts: tuple[TextPart | MediaPart, ...]
    tool_calls: tuple[ToolCallIdentity, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "MessageView":
        """解析公共消息；不认识的供应商字段明确拒绝，而非在转换中丢弃。"""
        role = message.get("role")
        fields = {
            "system": {"role", "content"},
            "user": {"role", "content", "media"},
            "assistant": {"role", "content", "tool_calls", "reasoning_content"},
            "tool": {"role", "content", "tool_call_id"},
        }
        if not isinstance(role, str) or role not in fields or message.keys() - fields[role]:
            raise ValueError("unsupported model message fields")
        text = message.get("content")
        calls = message.get("tool_calls")
        calls = [] if calls is None else calls
        if not isinstance(text, str) and not (role == "assistant" and text is None and calls):
            raise ValueError("invalid model message text")
        if role != "assistant" and "tool_calls" in message:
            raise ValueError("invalid tool calls role")
        if not isinstance(calls, list):
            raise ValueError("invalid tool calls")
        identities = []
        seen_ids: set[str] = set()
        for call in calls:
            if (
                not isinstance(call, dict)
                or call.keys() != {"id", "type", "function"}
                or call["type"] != "function"
                or not isinstance(call["id"], str)
                or not call["id"]
                or call["id"] in seen_ids
                or not isinstance(call["function"], dict)
                or call["function"].keys() != {"name", "arguments"}
                or any(not isinstance(call["function"][key], str) for key in ("name", "arguments"))
                or not call["function"]["name"]
            ):
                raise ValueError("invalid tool call identity")
            seen_ids.add(call["id"])
            identities.append(ToolCallIdentity(call["id"], **call["function"]))
        tool_id = message.get("tool_call_id")
        if role == "tool" and (not isinstance(tool_id, str) or not tool_id):
            raise ValueError("missing tool result identity")
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError("unsupported reasoning field")
        parts: list[TextPart | MediaPart] = [TextPart(text)] if isinstance(text, str) else []
        parts.extend(MediaPart(reference) for reference in message_media(message))
        return cls(role, tuple(parts), tuple(identities), tool_id, reasoning)


@dataclass(frozen=True)
class ModelCapabilities:
    """宿主对确切模型的声明，不根据模型名猜测；缺省只有原文字/工具/SSE 能力。"""

    model: str
    input_media_types: tuple[str, ...] = ()
    max_media_parts: int = 4
    max_media_bytes: int = 10485760
    tools: bool = True
    streaming: bool = True
    reasoning: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("invalid capability model")
        if (
            not isinstance(self.input_media_types, tuple)
            or any(not isinstance(kind, str) for kind in self.input_media_types)
            or len(set(self.input_media_types)) != len(self.input_media_types)
            or any(
                kind not in {"image/png", "image/jpeg", "image/webp"}
                for kind in self.input_media_types
            )
            or type(self.max_media_parts) is not int
            or not 1 <= self.max_media_parts <= 16
            or type(self.max_media_bytes) is not int
            or not 1 <= self.max_media_bytes <= 100000000
            or any(
                type(value) is not bool for value in (self.tools, self.streaming, self.reasoning)
            )
        ):
            raise ValueError("invalid model capabilities")

    def require_media(self, references: tuple[MediaRef, ...], model: str) -> None:
        """不支持、模型变化或总量超限时在读取正文/发送前拒绝，不静默退回纯文字。"""
        if not references:
            return
        if model != self.model or any(
            ref.mime_type not in self.input_media_types for ref in references
        ):
            raise AgentError("当前模型未声明支持这些媒体输入；未丢弃媒体或发送纯文字替代请求。")
        if (
            len(references) > self.max_media_parts
            or sum(ref.size_bytes for ref in references) > self.max_media_bytes
        ):
            raise AgentError("媒体输入数量或总字节超过模型适配器上限，未发送请求。")


class MediaInputValidator(Protocol):
    """应用在登记本轮前验证已保存引用；离线实现只消费已验证的冻结证据。"""

    def validate(self, references: tuple[MediaRef, ...], model: str) -> None:
        """不支持、来源/正文不符时抛 AgentError，不创建新会话或请求。"""
        ...


def message_media(message: dict[str, Any]) -> tuple[MediaRef, ...]:
    """严格解码有界用户媒体引用；其他角色不可偷带附件或供应商二进制数据。"""
    if "media" not in message:
        return ()
    items = message["media"]
    if message.get("role") != "user" or not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValueError("invalid message media")
    return tuple(decode_media_reference(item) for item in items)


def media_records(references: tuple[MediaRef, ...]) -> list[dict[str, Any]]:
    """保留输入顺序和完整来源；不把路径、外部 URL 或媒体字节写进消息。"""
    if (
        not isinstance(references, tuple)
        or len(references) > 16
        or any(not isinstance(ref, MediaRef) for ref in references)
    ):
        raise ValueError("invalid media references")
    return [asdict(reference) for reference in references]


def declared_capabilities(client: object, model: str) -> ModelCapabilities:
    """兼容旧文字客户端；未实现声明端口的对象不得获得媒体权限。"""
    value = getattr(client, "capabilities", None)
    return value if isinstance(value, ModelCapabilities) else ModelCapabilities(model)

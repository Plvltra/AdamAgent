"""Chat Completions 输入编码；媒体正文只在发送边界变为供应商数据 URL。"""

import base64
import hashlib
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from adam.contracts.errors import AgentError, FailureCode, LLMError
from adam.contracts.media import MediaContent, MediaRef
from adam.contracts.model_input import MediaPart, MessageView, ModelCapabilities, TextPart

MediaReader = Callable[[MediaRef], MediaContent]


class ChatInputEncoder:
    """只消费宿主声明与有来源的读取端口；缺省文字模型不能被输入内容升级为视觉模型。"""

    def __init__(
        self, capabilities: ModelCapabilities, read_media: MediaReader | None = None
    ) -> None:
        self.capabilities, self._read_media = capabilities, read_media

    def encode(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        has_tools: bool,
        stream: bool,
    ) -> list[dict[str, Any]]:
        """先校验整批能力和引用，再取得固定字节；本地失败结算为零且不自动重试。

        原文字/工具/推理字段逐值保留；只转换公共 user.media。数据 URL 仅供传输，
        不写会话或录制。此处验证消息格式与正文完整性，图片解码交给 C04。
        """
        try:
            views = tuple(MessageView.from_message(message) for message in messages)
            references = tuple(
                part.reference
                for view in views
                for part in view.parts
                if isinstance(part, MediaPart)
            )
            self.capabilities.require_media(references, model)
            if (
                has_tools
                and not self.capabilities.tools
                or stream
                and not self.capabilities.streaming
            ):
                raise AgentError("模型适配器未声明支持本次工具或流式请求。")
            if not self.capabilities.reasoning and any(
                view.reasoning is not None for view in views
            ):
                raise AgentError("模型适配器不能无损保留当前推理字段，未发送请求。")
            result = deepcopy(messages)
            for message, view in zip(result, views, strict=True):
                if "media" not in message:
                    continue
                del message["media"]
                parts: list[dict[str, Any]] = []
                for part in view.parts:
                    if isinstance(part, TextPart):
                        parts.append({"type": "text", "text": part.text})
                    else:
                        content = self._content(part.reference)
                        encoded = base64.b64encode(content.data).decode("ascii")
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{part.reference.mime_type};base64,{encoded}"
                                },
                            }
                        )
                message["content"] = parts
            return result
        except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
            message = (
                str(error) if isinstance(error, AgentError) else "模型输入格式或媒体来源无效。"
            )
            raise LLMError(
                message + " 本地校验停止，未发送请求。",
                (0, 0),
                code=FailureCode.MODEL_INPUT_INVALID,
            ) from None

    def _content(self, reference: MediaRef) -> MediaContent:
        """核对实际读取结果与完整引用；自报 MIME、摘要或文件存在不替代所属会话授权。"""
        if self._read_media is None:
            raise AgentError("模型适配器没有媒体读取端口。")
        content = self._read_media(reference)
        if (
            not isinstance(content, MediaContent)
            or content.reference != reference
            or not isinstance(content.data, bytes)
            or len(content.data) != reference.size_bytes
            or hashlib.sha256(content.data).hexdigest() != reference.version
        ):
            raise AgentError("媒体正文、版本或所有者与请求引用不符。")
        return content

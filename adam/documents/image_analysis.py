"""图片分析的来源与能力边界；当前不附带模型后端、路径读取、OCR 或 PDF 渲染。"""

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from adam.contracts.errors import AgentError
from adam.contracts.perception import ImageAnalyzer, ImageReply, ImageRequest
from adam.documents.models import DocumentError


@dataclass(frozen=True)
class ImageInspection:
    """固定字节版本对应的分析或明确能力不足；结果不包含图片字节，也不是引用原文。"""

    status: Literal["analyzed", "unavailable"]
    source_version: str
    size_bytes: int
    mime_type: str
    question_sha256: str
    reply: ImageReply | None
    reason: str | None = None
    independently_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        """输出来源与分析，未知用量原样保留；调用方另按自己的工具输出限额编码。"""
        return asdict(self)


def inspect_image(
    data: bytes,
    mime_type: str,
    question: str,
    *,
    analyzer: ImageAnalyzer | None = None,
    max_input_bytes: int = 1048576,
    max_output_chars: int = 4000,
    check_cancel: Callable[[], None] | None = None,
) -> ImageInspection:
    """给已读取图片绑定版本，再调用显式分析器；未提供时返回 unavailable，不猜测画面。

    调用者先进行路径授权和有界读取。此处只接受 PNG/JPEG/WebP 的 MIME 声明，
    不解码、不证明格式正确；具体后端还须验证格式及解码资源。输入最多8 MiB。
    图片是待分析数据，不是新指令；结果有出处不等于推断正确，也不代表完成 OCR。
    当前生产组装未配置分析器，接入付费模型前必须复用原请求日志、额度及未知用量处理。
    """
    cancel = check_cancel or (lambda: None)
    cancel()
    if (
        type(max_input_bytes) is not int
        or not 1 <= max_input_bytes <= 8 * 1024 * 1024
        or type(max_output_chars) is not int
        or not 1 <= max_output_chars <= 8000
    ):
        raise DocumentError("图片输入或分析输出上限无效。")
    if (
        not isinstance(data, bytes)
        or not 1 <= len(data) <= max_input_bytes
        or not isinstance(mime_type, str)
        or mime_type not in {"image/png", "image/jpeg", "image/webp"}
        or not isinstance(question, str)
        or not question.strip()
        or len(question) > 2000
    ):
        raise DocumentError("图片字节、MIME 声明或问题格式无效。")
    version = hashlib.sha256(data).hexdigest()
    try:
        question_version = hashlib.sha256(question.encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise DocumentError("图片分析问题不是有效 UTF-8 文本。") from error
    if analyzer is None:
        return ImageInspection(
            "unavailable",
            version,
            len(data),
            mime_type,
            question_version,
            None,
            "image_backend_not_configured",
        )
    request = ImageRequest(data, mime_type, version, question, max_output_chars)
    try:
        cancel()
        reply = analyzer.analyze(request, cancel)
        cancel()
    except AgentError:
        # 原请求的取消、额度及落盘故障不能变成模型可忽略的图片描述。
        raise
    except Exception as error:
        raise DocumentError("图片分析后端失败，未返回内容，也未自动重试。") from error
    _validate_reply(reply, max_output_chars)
    return ImageInspection("analyzed", version, len(data), mime_type, question_version, reply)


def _validate_reply(reply: ImageReply, maximum: int) -> None:
    """拒绝缺失模型身份、无界文本或伪造的用量类型；None 表示后端没有提供用量。"""
    if (
        not isinstance(reply, ImageReply)
        or not isinstance(reply.text, str)
        or not reply.text.strip()
        or len(reply.text) > maximum
        or not isinstance(reply.model, str)
        or not reply.model.strip()
        or len(reply.model) > 200
        or (
            reply.usage is not None
            and (
                not isinstance(reply.usage, tuple)
                or len(reply.usage) != 2
                or any(type(value) is not int or value < 0 for value in reply.usage)
            )
        )
    ):
        raise DocumentError("图片分析后端返回的内容、模型身份或用量无效。")
    try:
        reply.text.encode("utf-8")
        reply.model.encode("utf-8")
    except UnicodeError as error:
        raise DocumentError("图片分析后端返回了无法保存的文本。") from error

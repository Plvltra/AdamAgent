"""图片分析扩展契约；不持有文件权限、网络客户端或聊天会话实现。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ImageRequest:
    """已授权且有界的原字节；MIME 是声明，不证明图像可解码或内容已被理解。"""

    data: bytes = field(repr=False)
    mime_type: str
    source_version: str
    question: str
    max_output_chars: int


@dataclass(frozen=True)
class ImageReply:
    """后端分析与实际模型；usage 为输入/输出 token，缺失保持未知，不能补零或当作事实验证。"""

    text: str
    model: str
    usage: tuple[int, int] | None


class ImageAnalyzer(Protocol):
    """由宿主显式注入的分析器；远端实现须接入原请求记录、额度与取消，不能隐式调用。"""

    def analyze(self, request: ImageRequest, check_cancel: Callable[[], None]) -> ImageReply:
        """最多执行一次分析并返回有界结果；取消、日志或额度故障须原样传播，不自动重试。"""
        ...

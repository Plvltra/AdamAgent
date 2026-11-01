"""图片解码的局部值对象；格式与方向描述原始证据，不代表视觉识别结果。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef

IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImageError(Exception):
    """可交付的固定错误分类；message 不能包含解码器原文或任意文件内容。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class ImageLimits:
    """限制输入字节、解码像素和单边尺寸；不是进程内存沙箱或硬取消期限。"""

    max_bytes: int = 10485760
    max_pixels: int = 16000000
    max_dimension: int = 8192

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_bytes", 10485760),
            ("max_pixels", 64000000),
            ("max_dimension", 16384),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid image limit")


@dataclass(frozen=True)
class ImageInfo:
    """原始像素尺寸及 EXIF 方向；无方向为 None，display 尺寸按方向换轴，不重编码。"""

    format: str
    width: int
    height: int
    orientation: int | None

    def __post_init__(self) -> None:
        if self.format not in IMAGE_MIMES or any(
            type(value) is not int or value <= 0 for value in (self.width, self.height)
        ):
            raise ValueError("invalid decoded image information")
        if self.orientation is not None and (
            type(self.orientation) is not int or not 1 <= self.orientation <= 8
        ):
            raise ValueError("invalid EXIF orientation")

    @property
    def mime_type(self) -> str:
        return IMAGE_MIMES[self.format]

    @property
    def display_size(self) -> tuple[int, int]:
        """方向 5～8 交换显示轴；这不是对原图片执行变换。"""
        return (
            (self.height, self.width)
            if self.orientation in (5, 6, 7, 8)
            else (self.width, self.height)
        )


@dataclass(frozen=True)
class LoadedImage:
    """完整原始字节的已提交引用与对应解码元数据；不包含任何 OCR 或模型回答。"""

    media: MediaRef
    image: ImageInfo


class ImageDecoder(Protocol):
    """对完整字节作有界解码；取消和明确损坏不能降级成仅信任文件扩展名。"""

    def decode(self, data: bytes, *, cancel: CancellationToken) -> ImageInfo:
        """返回实际格式/尺寸/方向；不支持、损坏和超限抛出 ImageError。"""
        ...

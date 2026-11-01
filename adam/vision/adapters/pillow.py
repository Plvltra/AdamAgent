"""Pillow 单帧 PNG/JPEG/WebP 解码；不改变全局像素阈值或容忍截断的开关。"""

import io
import struct

from adam.contracts.cancellation import CancellationToken
from adam.vision.models import IMAGE_MIMES, ImageError, ImageInfo, ImageLimits


class PillowDecoder:
    """先检查头部资源边界，再校验文件和加载像素；不向业务层返回 Pillow 对象。"""

    def __init__(self, limits: ImageLimits) -> None:
        self._limits = limits

    def decode(self, data: bytes, *, cancel: CancellationToken) -> ImageInfo:
        """先检查实际格式、尺寸与单帧，再完整解码并读取方向；不信任路径后缀或 MIME。

        原生解码期间不能强制中止；取消在读取后的各阶段复检。像素和字节上限
        限制可接受输入，但不保证解码器的峰值内存或抵御原生库漏洞。
        """
        cancel.check()
        if not isinstance(data, bytes) or not data:
            raise ImageError("corrupt_image", "图片为空或不是完整字节。")
        if len(data) > self._limits.max_bytes:
            raise ImageError("byte_limit", "图片字节超过上限，未解码或保存。")
        try:
            from PIL import Image, ImageFile, UnidentifiedImageError
        except ImportError:
            raise ImageError(
                "dependency_missing", "图片解码未安装 Pillow；请显式安装 vision 可选依赖。"
            ) from None
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            raise ImageError(
                "unsafe_decoder_settings", "解码器被配置为容忍截断，拒绝作为完整图片输入。"
            )
        try:
            # verify 检查完整文件，load 实际解码；仅识别出头部不代表可用图片。
            with io.BytesIO(data) as stream, Image.open(stream, formats=list(IMAGE_MIMES)) as image:
                self._info(image.format, image.size, None)
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageError("animated_image", "暂不支持多帧或动画图片，未保存。")
                image.verify()
            cancel.check()
            with io.BytesIO(data) as stream, Image.open(stream, formats=list(IMAGE_MIMES)) as image:
                image.load()
                info = self._info(image.format, image.size, image.getexif().get(274))
            cancel.check()
            return info
        except UnidentifiedImageError:
            raise ImageError(
                "unsupported_or_corrupt", "不是可识别的单帧 PNG/JPEG/WebP，或图片已损坏。"
            ) from None
        except Image.DecompressionBombError:
            raise ImageError("pixel_limit", "图片解码像素超过安全上限，未保存。") from None
        except (OSError, ValueError, SyntaxError, struct.error, OverflowError):
            raise ImageError("corrupt_image", "图片内容、像素或方向信息损坏，未保存。") from None

    def _info(
        self, image_format: str | None, size: tuple[int, int], orientation: object
    ) -> ImageInfo:
        """校验尺寸与方向；调用方先以无方向元数据挡住过大分配，完整解码后再取方向。"""
        width, height = size
        if max(width, height) > self._limits.max_dimension:
            raise ImageError("dimension_limit", "图片单边尺寸超过上限，未解码或保存。")
        if width * height > self._limits.max_pixels:
            raise ImageError("pixel_limit", "图片像素总数超过上限，未解码或保存。")
        if orientation is not None and (type(orientation) is not int or not 1 <= orientation <= 8):
            raise ImageError("corrupt_image", "图片 EXIF 方向无效，未保存。")
        return ImageInfo(image_format or "", width, height, orientation)

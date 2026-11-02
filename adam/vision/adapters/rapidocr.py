"""本地 OCR ：已授权图片字节到原像素坐标和原生置信度。"""

import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.vision.adapters.pillow import PillowDecoder
from adam.vision.models import ImageError, ImageLimits


@dataclass(frozen=True)
class OcrLine:
    """四点坐标相对于原图像素；score 为后端原生分数而非正确率。"""

    text: str
    polygon: tuple[tuple[float, float], ...]
    score: float | None


@dataclass(frozen=True)
class OcrResult:
    """识别结果绑定原字节；空行只表示没有识别出文字。"""

    version: str
    width: int
    height: int
    lines: tuple[OcrLine, ...]
    backend: str = "rapidocr-3.9.2"


class LocalOcr:
    """只使用显式本地模型目录，不下载模型；每个实例串行使用。"""

    def __init__(self, models: Path, limits: ImageLimits | None = None) -> None:
        self.models = models
        self.decoder = PillowDecoder(limits or ImageLimits())
        self.engine: Any = None

    def recognize(self, data: bytes, cancel: CancellationToken) -> OcrResult:
        """完整解码后识别，返回原像素坐标；原生推理期间只能协作取消。

        EXIF 非默认方向须先由调用方显式生成并保存正向证据图，避免坐标指向不同图像。
        结果过多或非法时整次失败，不返回截断且貌似完整的文字。
        """
        info = self.decoder.decode(data, cancel=cancel)
        if info.orientation not in (None, 1):
            raise ImageError("orientation", "请先提供已保存的正向图片证据。")
        if self.engine is None:
            from rapidocr import RapidOCR

            files = {
                "Det": "PP-OCRv6_det_small.onnx",
                "Rec": "PP-OCRv6_rec_small.onnx",
                "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            }
            if any(not (self.models / name).is_file() for name in files.values()):
                raise ImageError("model_missing", "本地 OCR 模型文件缺失。")
            self.engine = RapidOCR(
                params={
                    **{key + ".model_path": str(self.models / name) for key, name in files.items()},
                    "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                }
            )
        from PIL import Image

        cancel.check()
        with Image.open(io.BytesIO(data)) as image:
            output = self.engine(image.convert("RGB"))
        cancel.check()
        lines = []
        if output.txts is not None:
            if len(output.txts) > 1000 or sum(map(len, output.txts)) > 32000:
                raise ImageError("output_limit", "OCR 文字超过上限。")
            scores = output.scores if output.scores is not None else [None] * len(output.txts)
            for text, box, score in zip(output.txts, output.boxes, scores, strict=True):
                polygon = tuple((float(x), float(y)) for x, y in box)
                confidence = None if score is None else float(score)
                if (
                    len(polygon) != 4
                    or any(not math.isfinite(v) for point in polygon for v in point)
                    or confidence is not None
                    and not 0 <= confidence <= 1
                ):
                    raise ImageError("invalid_result", "OCR 返回非法坐标或分数。")
                lines.append(OcrLine(text, polygon, confidence))
        return OcrResult(hashlib.sha256(data).hexdigest(), info.width, info.height, tuple(lines))

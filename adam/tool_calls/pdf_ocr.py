"""混合 PDF OCR ：B17 原文本与原页图证据分开交付。"""

import io
import math
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource
from adam.documents.pdf import PdfLimits, extract_pdf
from adam.vision.adapters.rapidocr import LocalOcr


def recognize_pdf(
    data: bytes,
    source: MediaSource,
    engine: LocalOcr,
    publish: Callable[[MediaSource, bytes, str], MediaRef],
    cancel: CancellationToken,
    *,
    start_page: int = 1,
    page_count: int = 3,
) -> dict[str, Any]:
    """识别最多三页；每个含图或无文本页保留渲染图，坐标属于该图像素。

    调用方先授权并限制 PDF 原字节。文本提取失败不降级成空结果；识别或发布失败
    直接传播，可能已有先前页面证据，不能声称没有副作用或自动重试。
    """
    if type(page_count) is not int or not 1 <= page_count <= 3:
        raise ValueError("OCR page_count must be 1..3")
    original = extract_pdf(
        data,
        start_page=start_page,
        page_count=page_count,
        limits=PdfLimits(max_read_pages=3),
        check_cancel=cancel.check,
    )
    import pypdfium2 as pdfium

    pages = []
    with pdfium.PdfDocument(data) as document:
        for extracted in original.pages:
            cancel.check()
            result: dict[str, Any] = {
                "page": extracted.page,
                "text_layer": asdict(extracted),
                "ocr": None,
                "image": None,
            }
            if extracted.has_images or not extracted.text.strip():
                page: Any = document[extracted.page - 1]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                        raise ValueError("Invalid PDF page dimensions")
                    scale = min(2.0, 2000 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        with bitmap.to_pil() as image, io.BytesIO() as stream:
                            image.save(stream, format="PNG")
                            png = stream.getvalue()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                cancel.check()
                recognized = engine.recognize(png, cancel)
                cancel.check()
                evidence = publish(source, png, "image/png")
                result.update(
                    ocr=asdict(recognized),
                    image=asdict(evidence),
                    coordinate_space="rendered_page_pixels",
                    pdf_size_points=[width, height],
                )
            pages.append(result)
    cancel.check()
    return {
        "version": original.version,
        "total_pages": original.total_pages,
        "next_page": original.next_page,
        "pages": pages,
        "warning": "OCR 为自动识别文字，可能错误；原文本层与识别结果分别保留。",
    }

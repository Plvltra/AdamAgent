"""从已授权且有界的 PDF 字节提取物理页文本；不读取路径、执行动作或进行 OCR。"""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

from pypdf import Configuration, PdfReader, apply_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import DictionaryObject, PdfObject

from adam.documents.models import DocumentError


@dataclass(frozen=True)
class PdfLimits:
    """字节为原文件或解码流大小，字符为提取文本长度；这些不是进程总内存硬上限。"""

    max_file_bytes: int = 8 * 1024 * 1024
    max_pages: int = 512
    max_read_pages: int = 20
    max_stream_bytes: int = 2 * 1024 * 1024
    max_page_chars: int = 8000
    max_total_chars: int = 32000

    def __post_init__(self) -> None:
        ceilings = (32 * 1024 * 1024, 2048, 100, 8 * 1024 * 1024, 100000, 1000000)
        if any(
            type(value) is not int or not 1 <= value <= ceiling
            for value, ceiling in zip(asdict(self).values(), ceilings, strict=True)
        ):
            raise ValueError("PDF 限制必须为正整数且不超过支持范围。")


@dataclass(frozen=True)
class PdfPage:
    """物理页码从1开始；无文本不代表空白，图像内容与版面始终未验证。"""

    page: int
    text: str
    status: str
    has_images: bool
    needs_ocr: bool
    extracted_chars: int
    truncated: bool


@dataclass(frozen=True)
class PdfExtract:
    """version 绑定完整原文件字节；next_page 只续读后续页，不补回页内截断文字。"""

    version: str
    total_pages: int
    pages: tuple[PdfPage, ...]
    next_page: int | None
    layout_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回可落盘的来源与页面证据；不把书上印刷页码推断为物理页码。"""
        return asdict(self)


def extract_pdf(
    data: bytes,
    *,
    start_page: int = 1,
    page_count: int = 5,
    limits: PdfLimits | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> PdfExtract:
    """按物理页返回有界文本，拒绝加密、畸形、超限或范围无效的文件。

    调用者先校验读取权限并限制原文件读取；此接口不打开路径或下载内容。
    部分页含图或只有图时明确标记，截断保留原提取字符数。任一页解析失败整次失败。
    取消在页面和文本操作边界检查；第三方解析前置步骤不保证即时取消或硬超时。
    """
    budget = limits or PdfLimits()
    cancel = check_cancel or (lambda: None)
    if (
        type(start_page) is not int
        or start_page < 1
        or type(page_count) is not int
        or not 1 <= page_count <= budget.max_read_pages
    ):
        raise DocumentError("PDF 起始物理页码或页数范围无效。")
    with _open_pdf(data, budget, cancel) as reader:
        total = len(reader.pages)
        if start_page > total:
            raise DocumentError("PDF 起始物理页码超过总页数。")
        pages = _pages(reader, start_page, page_count, budget, cancel)
    cancel()
    following = pages[-1].page + 1
    return PdfExtract(
        hashlib.sha256(data).hexdigest(),
        total,
        tuple(pages),
        following if following <= total else None,
    )


def extract_pdf_all(
    data: bytes, *, limits: PdfLimits, check_cancel: Callable[[], None]
) -> PdfExtract:
    """为索引一次解析全部物理页；任何文字截断或未读完都失败，不缓存部分原文。

    复用页树、解码及取消限制；图像没有转成文字，会保留页面能力标记。
    max_read_pages 只约束交互分页，本接口受 max_pages 和总文本字符上限约束。
    """
    with _open_pdf(data, limits, check_cancel) as reader:
        total = len(reader.pages)
        pages = _pages(reader, 1, total, limits, check_cancel)
        if len(pages) != total or any(page.truncated for page in pages):
            raise DocumentError("PDF 提取文字超过单页或总字符上限，未建立部分索引。")
    check_cancel()
    return PdfExtract(hashlib.sha256(data).hexdigest(), total, tuple(pages), None)


@contextmanager
def _open_pdf(data: bytes, budget: PdfLimits, cancel: Callable[[], None]) -> Iterator[PdfReader]:
    """统一打开及解析错误边界，读取和索引在同一组局部限制内使用 reader。"""
    cancel()
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise DocumentError("不是支持的 PDF 文件，未提取内容。")
    if len(data) > budget.max_file_bytes:
        raise DocumentError("PDF 原文件字节数超限，请缩小文件。")
    try:
        # ContextVar 作用域只影响本次解析，不修改其他主/子任务的库级全局限制。
        with apply_configuration(_configuration(budget)):
            reader = PdfReader(
                BytesIO(data),
                strict=True,
                root_object_recovery_limit=1000,
                xref_stream_maximum_entries=50000,
            )
            if reader.is_encrypted:
                raise DocumentError("PDF 已加密，本版不接收密码或自动解密。")
            total = len(reader.pages)
            if not total or total > budget.max_pages:
                raise DocumentError("PDF 页数为空或超限，未返回部分文档。")
            yield reader
    except DocumentError:
        raise
    except LimitReachedError as error:
        raise DocumentError("PDF 解码或结构复杂度超限，未返回部分内容。") from error
    except Exception as error:
        # 不把库异常中的文件原文、对象内容或解密细节发送给模型。
        raise DocumentError("PDF 结构损坏或文本提取失败，未返回部分内容。") from error


def _configuration(limits: PdfLimits) -> Configuration:
    """约束页树、解码和操作数，不解码图像或启动外部 JBIG2 程序。"""
    return Configuration(
        maximum_declared_stream_length=limits.max_stream_bytes,
        array_based_stream_maximum_output_length=limits.max_stream_bytes,
        zlib_maximum_output_length=limits.max_stream_bytes,
        lzw_maximum_output_length=limits.max_stream_bytes,
        run_length_maximum_output_length=limits.max_stream_bytes,
        brotli_maximum_output_length=limits.max_stream_bytes,
        stream_decoding_work_maximum_length=limits.max_stream_bytes * 2,
        stream_parsing_work_maximum_count=100000,
        stream_filters_maximum_length=4,
        array_maximum_element_count=10000,
        dictionary_maximum_element_count=10000,
        page_tree_maximum_entries=limits.max_pages * 4,
        page_tree_maximum_depth=32,
        page_tree_maximum_page_entries=limits.max_pages * 128,
        xform_maximum_invocations_per_extraction=128,
        jbig2dec_binary=None,
    )


def _pages(
    reader: PdfReader,
    start: int,
    count: int,
    limits: PdfLimits,
    cancel: Callable[[], None],
) -> list[PdfPage]:
    """每页独立截断且不跨页合并；总字符预算用完时留下明确续页位置。"""
    result = []
    remaining = limits.max_total_chars
    for number in range(start, min(len(reader.pages) + 1, start + count)):
        cancel()
        page = reader.pages[number - 1]
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > limits.max_stream_bytes:
            raise DocumentError("PDF 页面内容流超限，未返回部分内容。")
        has_images = _images(page.get("/Resources", {}), cancel)
        if contents is not None:
            has_images |= any(operator == b"INLINE IMAGE" for _, operator in contents.operations)

        def visit(operator: bytes, *_args: object) -> None:
            nonlocal has_images
            cancel()
            has_images |= operator == b"INLINE IMAGE"

        text = page.extract_text(visitor_operand_before=visit)
        cancel()
        status = "text" if text.strip() else "image_only" if has_images else "no_extractable_text"
        visible = text[: min(remaining, limits.max_page_chars)]
        result.append(
            PdfPage(
                number,
                visible,
                status,
                has_images,
                has_images and not text.strip(),
                len(text),
                len(visible) < len(text),
            )
        )
        remaining -= len(visible)
        if remaining == 0:
            break
    return result


def _images(resources: object, cancel: Callable[[], None]) -> bool:
    """只看资源类型，包含嵌套 Form；不把图片流加载为像素或解释嵌入附件。"""
    pending = [(resources, 0)]
    seen: set[int] = set()
    has_images = False
    while pending:
        cancel()
        raw, depth = pending.pop()
        resolved = raw.get_object() if isinstance(raw, PdfObject) else raw
        if not isinstance(resolved, dict) or id(resolved) in seen:
            continue
        seen.add(id(resolved))
        if depth > 16 or len(seen) > 256:
            raise DocumentError("PDF 资源结构超限，未返回部分内容。")
        objects = resolved.get("/XObject", {})
        objects = objects.get_object() if isinstance(objects, PdfObject) else objects
        if not isinstance(objects, dict):
            raise DocumentError("PDF 图像资源格式无效。")
        for value in objects.values():
            item = value.get_object()
            if not isinstance(item, DictionaryObject):
                raise DocumentError("PDF 图像资源格式无效。")
            has_images |= item.get("/Subtype") == "/Image"
            if item.get("/Subtype") == "/Form":
                pending.append((item.get("/Resources", {}), depth + 1))
    return has_images

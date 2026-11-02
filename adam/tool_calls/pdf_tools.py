"""按已授权路径读取 PDF 物理页；完整保留来源与截断信息，不自动 OCR 或联网。"""

import json
import re
import time
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.documents.models import DocumentError
from adam.documents.pdf import PdfLimits, extract_pdf
from adam.documents.pdf_source import read_pdf_snapshot
from adam.tool_calls.policy import PathPolicy


class PdfTools:
    """实例属于本次只读调用，取消与解析期限不在主子任务之间共享。"""

    def __init__(
        self, policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
    ) -> None:
        self.policy, self.limits, self.cancel = policy, limits, cancel

    def read_pdf(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回物理页和完整原文件版本；expected_version 可用于拒绝跨版本续读。

        先读取授权快照，提取后再次确认路径和元数据。协作超时在解析操作边界检查，
        不承诺强制中止第三方解析；输出过小则失败，不截断 JSON 来源结构。
        """
        expected = args.get("expected_version")
        if expected is not None and re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise DocumentError("PDF expected_version 必须是原样返回的64位版本。")
        deadline = time.monotonic() + self.limits.scan_timeout_seconds

        def check() -> None:
            if self.cancel is not None:
                self.cancel.check()
            if time.monotonic() > deadline:
                raise DocumentError("PDF 读取达到协作时间上限，未返回部分内容。")

        budget = PdfLimits(
            max_file_bytes=self.limits.max_pdf_file_bytes,
            max_read_pages=self.limits.max_pdf_read_pages,
            max_stream_bytes=self.limits.max_pdf_stream_bytes,
        )
        snapshot = read_pdf_snapshot(path, self.policy.resolve, budget, check)
        if expected is not None and snapshot.version != expected:
            raise DocumentError("PDF 已更新，未使用旧版本续读；请重新读取并核对来源。")
        found = extract_pdf(
            snapshot.data,
            start_page=args.get("start_page", 1),
            page_count=args.get("page_count", min(5, budget.max_read_pages)),
            limits=budget,
            check_cancel=check,
        )
        snapshot.verify(self.policy.resolve)
        check()
        result = {
            "status": "pages",
            "path": str(snapshot.path),
            **found.to_dict(),
            "hint": "引用物理页码和版本；图像与版面未验证，needs_ocr不表示已完成OCR。页内截断需重新读取该页。",
        }
        result["pages"] = list(result["pages"])
        return _pack(result, self.limits.max_result_chars)


def encode_pdf_result(result: dict[str, Any]) -> str:
    """成功结果已按相同序列化方式限长，禁止通用预览替换掉页码或续页位置。"""
    return json.dumps(result, ensure_ascii=False)


def _pack(result: dict[str, Any], maximum: int) -> dict[str, Any]:
    """优先保留完整前页；单页仍太长才缩短文字，保留实际提取长度和明确截断标记。"""
    while len(encode_pdf_result({"ok": True, **result})) > maximum:
        pages = result["pages"]
        if len(pages) > 1:
            removed = pages.pop()
            result["next_page"] = removed["page"]
        elif pages[0]["text"]:
            page = pages[0]
            page["text"] = page["text"][: len(page["text"]) // 2]
            page["truncated"] = True
        else:
            raise DocumentError("PDF 来源与页状态超过输出上限，请提高 max_result_chars。")
    return result

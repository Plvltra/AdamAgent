"""显式对指定 PDF 执行本地索引/读取/版本验收；只操作临时副本，不调用聊天或视觉模型。"""

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from adam.config.settings import DocumentConfig, PermissionConfig, ToolLimits
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.pdf import PdfLimits, extract_pdf
from adam.documents.pdf_source import read_pdf_snapshot
from adam.documents.search import DocumentSearch
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy


def verify(source: Path, query: str, *, max_stream_bytes: int = 2097152) -> dict[str, Any]:
    """核对真实全文页码、搜索到读取及旧版本拒绝，只返回统计，不保存书稿正文。

    source 是用户显式指定的单个文件；不会扫描其父目录。最大32 MiB、2048页和
    1000000提取字符，使用最长300秒的协作期限；失败不降低限制或改用替身。
    流限额默认2 MiB，须显式传参才可提高，最多8 MiB；不会根据失败自动调整。
    本接口验证工程行为，命中关键词不代表检索质量、模型引用质量或视觉理解通过。
    """
    started = perf_counter()
    source = source.resolve()
    policy = PathPolicy(source.parent, PermissionConfig())
    limits = PdfLimits(
        max_file_bytes=32 * 1024 * 1024, max_pages=2048, max_stream_bytes=max_stream_bytes
    )
    snapshot = read_pdf_snapshot(source, policy.resolve, limits, lambda: None)
    total = extract_pdf(snapshot.data, page_count=1, limits=limits).total_pages
    with tempfile.TemporaryDirectory(prefix="adam-pdf-acceptance-") as directory:
        # Windows 临时目录可能使用短文件名，先规范化再交给同一词法/真实路径授权策略。
        root = Path(directory).resolve()
        docs = root / "docs"
        docs.mkdir()
        copied = docs / "reference.pdf"
        copied.write_bytes(snapshot.data)
        config = DocumentConfig(
            roots=(docs,),
            max_file_bytes=limits.max_file_bytes,
            max_total_bytes=limits.max_file_bytes,
            max_pdf_pages=limits.max_pages,
            max_pdf_stream_bytes=max_stream_bytes,
            scan_timeout_seconds=300,
        )
        index = DocumentIndex(
            root / "cache", root, config, PathPolicy(root, PermissionConfig()).resolve
        )
        corpus = index.refresh()
        pages = {chunk.page for chunk in corpus.chunks} | {notice.page for notice in corpus.notices}
        if pages != set(range(1, total + 1)):
            raise DocumentError("全文索引没有为全部物理页保留文本或能力提示。")
        found = DocumentSearch(index).search(query, 3)
        if not found.hits:
            raise DocumentError("固定查询未找到原文，不能完成搜索到读取的验收。")
        first = found.hits[0].chunk
        registry = create_tool_registry(
            root,
            limits=ToolLimits(
                max_pdf_file_bytes=limits.max_file_bytes, max_pdf_stream_bytes=max_stream_bytes
            ),
        )
        arguments = {
            "path": str(copied),
            "start_page": first.page,
            "page_count": 1,
            "expected_version": first.version,
        }
        read = json.loads(registry.execute("read_pdf", json.dumps(arguments)))
        if (
            not read["ok"]
            or read["version"] != snapshot.version
            or read["pages"][0]["page"] != first.page
            or first.text not in read["pages"][0]["text"]
        ):
            raise DocumentError("读取未覆盖检索片段，请检查页码、版本或页面截断，不能标为通过。")
        # 仅为临时副本增加合法尾部空白，改变原字节版本而不改书稿内容。
        with copied.open("ab") as stream:
            stream.write(b"\n")
        stale = json.loads(registry.execute("read_pdf", json.dumps(arguments)))
        if stale["ok"] or "已更新" not in stale["error"]:
            raise DocumentError("旧版本续读没有被明确拒绝。")
        changed = index.refresh()
        expected_new = hashlib.sha256(snapshot.data + b"\n").hexdigest()
        if changed.generation == corpus.generation or any(
            c.version != expected_new for c in changed.chunks
        ):
            raise DocumentError("PDF 原字节改变后索引没有更新版本。")
        snapshot.verify(policy.resolve)
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "source_name": source.name,
            "source_version": snapshot.version,
            "source_bytes": len(snapshot.data),
            "physical_pages": total,
            "observed_pages": len(pages),
            "chunks": len(corpus.chunks),
            "indexed_chars": sum(len(c.text) for c in corpus.chunks),
            "notices": dict(Counter(n.kind for n in corpus.notices)),
            "query": query,
            "retrieved_pages": [hit.chunk.page for hit in found.hits],
            "retrieved_versions_match": all(
                hit.chunk.version == snapshot.version for hit in found.hits
            ),
            "read_matches_first_fragment": True,
            "old_version_rejected": True,
            "changed_copy_version": expected_new,
            "copy_generation_changed": True,
            "source_metadata_unchanged": True,
            "limits": {**asdict(config), "roots": ["temporary_copy_only"]},
            "elapsed_seconds": round(perf_counter() - started, 3),
            "real_pdf_parser": True,
            "paid_model_requests": 0,
            "visual_analysis": "not_performed",
            "retrieval_quality": "not_scored",
        }


def main() -> int:
    """排他保存不含正文的报告；已存在报告不覆盖，真实处理失败直接报告错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--query", default="函数调用")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-stream-bytes", type=int, default=2097152)
    args = parser.parse_args()
    if args.output.exists():
        raise DocumentError("输出报告已经存在，未覆盖。")
    result = verify(args.pdf, args.query, max_stream_bytes=args.max_stream_bytes)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("真实 PDF 全文页级索引、同版读取及版本更新验收通过；视觉效果未验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

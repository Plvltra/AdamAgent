"""固定合成资料对照全库摘录概览与单点检索，不调用模型或读取真实用户文件。"""

import argparse
from pathlib import Path
from typing import Any

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import AgentError
from adam.documents.catalog_query import KnowledgeDirectory
from adam.documents.evaluation import evaluate
from adam.documents.evidence_audit import audit_summary, quote_chunk
from adam.documents.index import DocumentIndex
from adam.documents.search import DocumentSearch
from adam.tool_calls.policy import PathPolicy
from scripts.injection_capture import write_record

DOCUMENTS = {
    "network-a.md": "# Network\n\nORION 断线最多重试两次。\n\nORION 权限失败禁止重试。\n\nAUDIT_CODE 是 KAPPA-731。\n",
    "network-b.md": "# network\n\nNOVA 断线后需要用户手动重试。\n\n下载任务有效期为二十四小时。\n",
    "storage.md": "# Storage\n\n只有完整回答保存成功，才可判定轮次成功。\n",
    "deploy.md": "# Deployment\n\nCANARY 首批发布比例为百分之十。\n",
    "limits.md": "# Limits\n\n子任务最多并行两个。\n",
}
QUESTIONS = (
    ("retry", "ORION 断线重试", "network-a.md", 3),
    ("deep-detail", "AUDIT_CODE KAPPA", "network-a.md", 7),
    ("saved", "完整回答保存成功", "storage.md", 3),
    ("rollout", "CANARY 首批发布比例", "deploy.md", 3),
)


def run_suite(directory: Path) -> dict[str, Any]:
    """建立不可覆盖的独立实验，保存目录全页、真实原文引用审计和同语料单点指标。

    这里的全库总结是按主题摘录的确定性基线，不是模型生成。文件/主题覆盖率只说明
    哪些来源被代表，不证明所有事实被总结；单点 Recall 也不衡量概览完整性。
    """
    directory.mkdir(parents=True, exist_ok=False)
    docs = directory / "docs"
    docs.mkdir()
    for name, text in DOCUMENTS.items():
        (docs / name).write_text(text, encoding="utf-8", newline="\n")
    index = DocumentIndex(
        directory / "cache",
        directory,
        DocumentConfig(roots=(Path("docs"),)),
        PathPolicy(directory, PermissionConfig()).resolve,
    )
    corpus = index.refresh()
    cases = []
    for identifier, query, name, line in QUESTIONS:
        chunk = next(c for c in corpus.chunks if Path(c.path).name == name and c.start_line == line)
        cases.append(
            {
                "id": identifier,
                "kind": "exact",
                "query": query,
                "relevant": [
                    {
                        "path": "docs/" + name,
                        "version": chunk.version,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "evidence": chunk.text,
                    }
                ],
            }
        )
    manifest = {
        "format": "knowledge_comparison_v1",
        "mode": "extractive",
        "model_requests": 0,
        "documents": DOCUMENTS,
        "cases": cases,
        "source_generation": corpus.generation,
    }
    write_record(directory / "manifest.json", manifest, exclusive=True)
    knowledge = KnowledgeDirectory(index)
    pages = [knowledge.overview(limit=20)]
    while pages[-1]["has_more"]:
        if pages[-1]["status"] == "output_limit":
            raise ValueError("知识对照输出预算不足，未生成完成报告。")
        pages.append(
            knowledge.overview(
                offset=pages[-1]["next_offset"],
                limit=20,
                catalog_version=pages[0]["catalog_version"],
            )
        )
    chunks = {c.chunk_id: c for c in corpus.chunks}
    topics = [topic for page in pages for topic in page["items"]]
    claims = [
        {
            "text": excerpt["text"],
            "citations": [
                quote_chunk(chunks[excerpt["chunk_id"]], start=excerpt["start"], end=excerpt["end"])
            ],
        }
        for topic in topics
        for excerpt in topic["overview"]
    ]
    summary = {"source_generation": corpus.generation, "claims": claims}
    audit = audit_summary(index, summary)
    single = evaluate(DocumentSearch(index).search, cases, 1)
    if (
        single["source_generation"] != corpus.generation
        or audit["source_generation"] != corpus.generation
    ):
        raise ValueError("资料在对照期间变化，未保存混合来源的报告。")
    network = next(topic for topic in topics if topic["title"].casefold() == "network")
    original = knowledge.read_topic(network["topic_id"], pages[0]["catalog_version"], limit=20)
    if original["has_more"]:
        raise ValueError("固定资料的主题详情未读取完整。")
    golden = next(c for c in corpus.chunks if "AUDIT_CODE" in c.text)
    report = {
        "format": "knowledge_comparison_v1",
        "mode": "extractive",
        "model_requests": 0,
        "global": {
            "pages": pages,
            "summary": summary,
            "audit": audit,
            "represented_topics": len(topics),
            "total_topics": pages[0]["topic_count"],
            "semantic_quality": "not_evaluated",
        },
        "single_point": single,
        "deep_detail": {
            "chunk_id": golden.chunk_id,
            "in_overview": any(golden.text in claim["text"] for claim in claims),
            "in_topic_originals": any(
                item["chunk_id"] == golden.chunk_id for item in original["items"]
            ),
            "originals": original,
        },
    }
    write_record(directory / "report.json", report, exclusive=True)
    return report


def main(argv: list[str] | None = None) -> int:
    """显式运行纯本地对照；目录必须全新，失败不会覆盖原始证据。"""
    parser = argparse.ArgumentParser(description="知识概览与单点检索的本地固定对照")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_suite(args.directory)
        print(f"已保存知识对照，单点题数 {len(report['single_point']['rows'])}，模型请求 0。")
        return 0
    except (AgentError, OSError, ValueError):
        print("知识对照失败；保留已有文件，请使用全新目录并检查来源与输出限制。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

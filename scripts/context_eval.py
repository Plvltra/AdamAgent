"""对合成的指代/缩写资料比较原文检索与语境增强；模型生成须显式 --model-generated。"""

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from adam.config.loader import load_llm_config
from adam.config.settings import DocumentConfig, LLMConfig, PermissionConfig
from adam.contracts.errors import AgentError
from adam.documents.contextualization import ContextBuilder
from adam.documents.evaluation import evaluate
from adam.documents.index import DocumentIndex
from adam.documents.search import DocumentSearch
from adam.documents.snapshots import digest
from adam.llm.client import LLMClient
from adam.tool_calls.policy import PathPolicy
from scripts.context_generation import ModelContextBuilder
from scripts.injection_capture import write_record

DOCUMENTS = {
    "orion.md": "ORION 是负责图片缓存的后台服务。\n\n它关闭后不会自动重启。\n\n过期条目会被立即丢弃。\n",
    "nova.md": "NOVA 是负责模型下载的后台服务。\n\n它关闭后会自动重启。\n",
    "rpo.md": "RPO 是允许丢失的数据时间窗口。\n\n本项目把它设为十五分钟。\n",
    "rto.md": "RTO 是允许中断服务的最长时长。\n\n本项目把它设为两小时。\n",
}
QUESTIONS = (
    ("pronoun-negative", "paraphrase", "ORION 关闭后会自动重启吗", "orion.md", 3),
    ("pronoun-positive", "paraphrase", "NOVA 关闭后是否自行重启", "nova.md", 3),
    ("abbreviation-rpo", "paraphrase", "RPO 允许丢失多久的数据", "rpo.md", 3),
    ("abbreviation-rto", "paraphrase", "RTO 允许停服多久", "rto.md", 3),
    ("literal-control", "exact", "过期条目立即丢弃", "orion.md", 5),
    ("unknown", "no_answer", "火星基地氧气配给", None, None),
)


def run_suite(
    directory: Path,
    *,
    llm: LLMConfig | None = None,
    max_known_tokens: int = 50000,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """在全新目录建立合成资料并留存两路结果；显式 llm 才发送资料，无工具或重试。

    transport 只用于模拟远端的工程测试。失败保留已落盘输入与基线，不产生成功汇总，
    也不自动继续中断的生成；已有目录拒绝覆盖。
    """
    if type(max_known_tokens) is not int or not 1 <= max_known_tokens <= 1000000:
        raise ValueError("token 上限须为 1～1000000。")
    directory.mkdir(parents=True, exist_ok=False)
    docs = directory / "docs"
    docs.mkdir()
    for name, text in DOCUMENTS.items():
        (docs / name).write_text(text, encoding="utf-8", newline="\n")
    config = DocumentConfig(roots=(Path("docs"),))
    index = DocumentIndex(
        directory / "cache", directory, config, PathPolicy(directory, PermissionConfig()).resolve
    )
    corpus = index.refresh()
    cases = []
    for identifier, kind, query, name, line in QUESTIONS:
        selected = next(
            (c for c in corpus.chunks if Path(c.path).name == name and c.start_line == line), None
        )
        cases.append(
            {
                "id": identifier,
                "kind": kind,
                "query": query,
                "relevant": [
                    {
                        "path": "docs/" + name,
                        "version": selected.version,
                        "start_line": selected.start_line,
                        "end_line": selected.end_line,
                        "evidence": selected.text,
                    }
                ]
                if selected and name
                else [],
            }
        )
    mode = ("mock_model" if transport is not None else "live_model") if llm else "extractive"
    write_record(
        directory / "manifest.json",
        {
            "format": "context_evaluation_v1",
            "mode": mode,
            "cases": cases,
            "corpus_generation": corpus.generation,
            "documents": DOCUMENTS,
            "prompt_and_sources_are_data": True,
            "max_known_tokens": max_known_tokens,
            "k": 1,
            "source_version": digest(DOCUMENTS),
        },
        exclusive=True,
    )
    try:
        baseline = evaluate(DocumentSearch(index).search, cases, 1)
        write_record(directory / "baseline.json", baseline, exclusive=True)
        with httpx.Client(transport=transport, trust_env=False) as http:
            builder: ContextBuilder | None = None
            if llm:
                builder = ModelContextBuilder(
                    LLMClient(llm, http),
                    llm.model,
                    directory / "requests",
                    max_known_tokens=max_known_tokens,
                )
            index.config = replace(config, contextualize=True)
            enhanced = evaluate(DocumentSearch(index, context_builder=builder).search, cases, 1)
        if baseline["source_generation"] != enhanced["source_generation"]:
            raise ValueError("source changed between comparisons")
        result = {
            "format": "context_evaluation_v1",
            "mode": mode,
            "baseline": baseline,
            "contextual": enhanced,
            "generation_requests": builder.count if isinstance(builder, ModelContextBuilder) else 0,
            "known_generation_tokens": builder.known_tokens
            if isinstance(builder, ModelContextBuilder)
            else 0,
            "unknown_usage": builder.unknown_usage
            if isinstance(builder, ModelContextBuilder)
            else False,
            "quality_notice": "检索指标不证明说明正确；必须核对否定、对象、缩写与原文。",
        }
        write_record(directory / "report.json", result, exclusive=True)
        return result
    except BaseException as error:
        write_record(
            directory / "failed.json",
            {
                "status": "failed",
                "error": error.code.value
                if isinstance(error, AgentError)
                else "interrupted_or_local",
                "automatic_retry": False,
            },
            exclusive=True,
        )
        raise


def main(argv: list[str] | None = None) -> int:
    """默认纯本地；只有显式模型开关及配置都提供，才允许发送合成资料并可能付费。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--model-generated",
        action="store_true",
        help="明确允许将合成片段发送给配置的 LLM，可能产生费用",
    )
    parser.add_argument("--llm-config", type=Path)
    parser.add_argument("--max-known-tokens", type=int, default=50000)
    args = parser.parse_args(argv)
    if args.model_generated != (args.llm_config is not None):
        parser.error("--model-generated 与 --llm-config 必须一起使用；默认不加载聊天连接。")
    try:
        result = run_suite(
            args.directory,
            llm=load_llm_config(args.llm_config) if args.llm_config else None,
            max_known_tokens=args.max_known_tokens,
        )
    except (AgentError, OSError, ValueError, KeyboardInterrupt):
        print("语境评估未完成；保留已有证据，不自动继续或重发。")
        return 1
    print(f"已保存 {result['mode']} 对照；生成请求 {result['generation_requests']} 次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""独立的文档查询、模型准备和评估入口，不加载聊天 API 密钥。"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from adam.config.loader import adam_home
from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import AgentError
from adam.documents.embedding import FastEmbedBackend
from adam.documents.evaluation import evaluate
from adam.documents.index import DocumentIndex
from adam.documents.search import DocumentSearch
from adam.tool_calls.document_tools import pack_results
from adam.tool_calls.policy import PathPolicy


def main(argv: list[str] | None = None) -> int:
    """运行独立文档操作；仅 prepare-model 子命令允许下载公开模型文件。"""
    parser = argparse.ArgumentParser(description="Adam 文档检索，不调用聊天 LLM")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, action="append")
    parser.add_argument("--allow-read-dir", type=Path, action="append", default=[])
    parser.add_argument("--cache-dir", type=Path, default=adam_home() / "documents")
    parser.add_argument("--model", default=DocumentConfig().embedding_model)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--minimum", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare-model", help="显式下载并验证本地 CPU embedding 模型")
    search = commands.add_parser("search", help="查询指定文档并返回可核查片段")
    search.add_argument("query")
    evaluation = commands.add_parser(
        "evaluate", help="运行固定题集；--semantic 同时比较关键词和向量"
    )
    evaluation.add_argument("questions", type=Path)
    args = parser.parse_args(argv)
    try:
        config = DocumentConfig(
            roots=tuple(args.root or [Path("docs")]),
            method="semantic" if args.semantic else "keyword",
            embedding_model=args.model,
            semantic_min_score=args.minimum,
        )
        if args.command == "prepare-model":
            FastEmbedBackend(
                args.cache_dir / "models", config.embedding_model, allow_download=True
            ).prepare()
            result = {"status": "ready", "model": config.embedding_model}
        else:
            permissions = PermissionConfig(
                additional_read_dirs=tuple(path.resolve() for path in args.allow_read_dir)
            )
            policy = PathPolicy(args.workspace, permissions, protected_dirs=(args.cache_dir,))
            index = DocumentIndex(args.cache_dir, args.workspace, config, policy.resolve)
            if args.command == "search":
                result = pack_results(DocumentSearch(index).search(args.query, args.limit), 8000)
            else:
                cases = json.loads(args.questions.read_text(encoding="utf-8-sig"))
                result = {
                    "metadata": {
                        "embedding_model": config.embedding_model if args.semantic else None,
                        "semantic_min_score": config.semantic_min_score,
                        "chunk_chars": config.chunk_chars,
                        "encoding": "window256-mean-v1" if args.semantic else None,
                    }
                }
                for method in ["keyword", "semantic"] if args.semantic else ["keyword"]:
                    selected = replace(config, method=method)
                    engine = DocumentSearch(
                        DocumentIndex(args.cache_dir, args.workspace, selected, policy.resolve)
                    )
                    result[method] = evaluate(engine.search, cases, args.limit)
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n", encoding="utf-8")
        else:
            print(encoded)
        return 0
    except (AgentError, OSError, ValueError) as error:
        message = (
            str(error)
            if isinstance(error, AgentError)
            else "文档操作失败，请检查路径和 JSON 格式。"
        )
        print(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

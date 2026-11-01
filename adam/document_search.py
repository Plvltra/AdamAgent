"""独立的文档查询、模型准备和评估入口，不加载聊天 API 密钥。"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from adam.config.loader import adam_home
from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import AgentError
from adam.contracts.reranking import RerankError
from adam.documents.embedding import FastEmbedBackend
from adam.documents.evaluation import evaluate
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.reranking import FastEmbedReranker
from adam.documents.search import DocumentSearch
from adam.tool_calls.document_tools import pack_results
from adam.tool_calls.policy import PathPolicy


def main(argv: list[str] | None = None) -> int:
    """运行独立文档操作；仅 prepare-model / prepare-reranker 允许下载公开模型文件。"""
    parser = argparse.ArgumentParser(description="Adam 文档检索，不调用聊天 LLM")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, action="append")
    parser.add_argument("--allow-read-dir", type=Path, action="append", default=[])
    parser.add_argument("--cache-dir", type=Path, default=adam_home() / "documents")
    parser.add_argument("--model", default=DocumentConfig().embedding_model)
    method = parser.add_mutually_exclusive_group()
    method.add_argument("--semantic", action="store_true")
    method.add_argument("--hybrid", action="store_true")
    parser.add_argument("--hybrid-candidates", type=int, default=20)
    parser.add_argument("--rrf-constant", type=int, default=60)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--reranker-model", default=DocumentConfig().reranker_model)
    parser.add_argument("--minimum", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare-model", help="显式下载并验证本地 CPU embedding 模型")
    commands.add_parser("prepare-reranker", help="显式下载并验证本地 CPU 精排模型")
    search = commands.add_parser("search", help="查询指定文档并返回可核查片段")
    search.add_argument("query")
    evaluation = commands.add_parser(
        "evaluate", help="固定题集；比较关键词/语义/融合，--rerank 另保留精排对照"
    )
    evaluation.add_argument("questions", type=Path)
    args = parser.parse_args(argv)
    try:
        config = DocumentConfig(
            roots=tuple(args.root or [Path("docs")]),
            method="hybrid" if args.hybrid else "semantic" if args.semantic else "keyword",
            embedding_model=args.model,
            semantic_min_score=args.minimum,
            hybrid_candidates=args.hybrid_candidates,
            rrf_constant=args.rrf_constant,
            rerank=args.rerank,
            reranker_model=args.reranker_model,
        )
        if args.command == "prepare-reranker":
            FastEmbedReranker(
                args.cache_dir / "rerankers", config.reranker_model, allow_download=True
            ).prepare()
            result = {"status": "ready", "reranker_model": config.reranker_model}
        elif args.command == "prepare-model":
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
                        "embedding_model": config.embedding_model
                        if args.semantic or args.hybrid
                        else None,
                        "semantic_min_score": config.semantic_min_score,
                        "chunk_chars": config.chunk_chars,
                        **(
                            {"rerank": True, "reranker_model": config.reranker_model}
                            if args.rerank
                            else {}
                        ),
                        "encoding": "window256-mean-v1" if args.semantic or args.hybrid else None,
                        **(
                            {
                                "hybrid_candidates": config.hybrid_candidates,
                                "rrf_constant": config.rrf_constant,
                            }
                            if args.hybrid
                            else {}
                        ),
                    }
                }
                methods = (
                    ["keyword", "semantic", "hybrid"]
                    if args.hybrid
                    else ["keyword", "semantic"]
                    if args.semantic
                    else ["keyword"]
                )
                routes = [(name, False) for name in methods]
                if args.rerank:
                    routes.append(("hybrid", True))
                generation = None
                for name, rerank in routes:
                    selected = replace(config, method=name, rerank=rerank)
                    engine = DocumentSearch(
                        DocumentIndex(args.cache_dir, args.workspace, selected, policy.resolve)
                    )
                    label = "hybrid_reranked" if rerank else name
                    report = evaluate(engine.search, cases, args.limit)
                    if generation is not None and report["source_generation"] != generation:
                        raise DocumentError("不同评估路线之间语料发生变化；请固定资料后重新运行。")
                    generation = report["source_generation"]
                    result[label] = report
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n", encoding="utf-8", newline="\n")
        else:
            print(encoded)
        return 0
    except RerankError:
        print("本地精排模型准备失败；请检查可选依赖、模型名称和下载连接。")
        return 1
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

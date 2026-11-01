"""用固定合成记忆比较关键词和本地语义召回；不调用聊天 API，不自动下载模型。"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from typing import Any

from adam.application.memories import MemoryService
from adam.config.settings import MemoryConfig
from adam.contracts.embedding import EmbeddingBackend
from adam.contracts.errors import AgentError
from adam.contracts.memory import MemoryRecord, MemoryReference, MemorySource, MemorySubject
from adam.documents.embedding import FastEmbedBackend
from adam.memory.project import project_identity
from adam.memory.semantic import SemanticMemoryRanker
from adam.memory.store import MemoryStore
from scripts.injection_capture import write_record


@dataclass(frozen=True)
class RecallCase:
    """期望是可核对的记忆身份集合，不把模型回答或相似度当作真值。"""

    case_id: str
    project: str
    query: str
    expected: tuple[str, ...] = ()
    ambiguity: bool = False


CASES = (
    RecallCase("exact", "alpha", "代码修改后运行什么检查？", ("test-rule",)),
    RecallCase("paraphrase", "alpha", "我动了几行实现，怎样确认没把已有行为弄坏？", ("test-rule",)),
    RecallCase("explicit-subject", "alpha", "@wang-dev 喜欢喝什么？", ("dev-tea",)),
    RecallCase("ambiguous-name", "alpha", "王伟喜欢喝什么？", ambiguity=True),
    RecallCase("changed-preference", "alpha", "@owner 我该用哪种语言来回复？", ("language-new",)),
    RecallCase("expired", "alpha", "临时连接口令是什么？"),
    RecallCase("future", "alpha", "未来发布代号是什么？"),
    RecallCase("project-isolation", "beta", "ALPHA 私有制品仓库地址是什么？"),
    RecallCase("shared", "beta", "跨项目沟通使用什么语言？", ("shared-language",)),
    RecallCase(
        "conflicting-current", "alpha", "@deployment 发布策略是什么？", ("rolling", "blue-green")
    ),
)


def seed_memories(directory: Path) -> tuple[MemoryStore, dict[str, Path], dict[str, MemoryRecord]]:
    """只在全新评估目录内创建两项目、全局及有冲突/时间关系的记忆，保留原文件供查证。"""
    roots = {name: directory / name for name in ("alpha", "beta")}
    for root in roots.values():
        root.mkdir()
    store = MemoryStore(directory / "memories")
    records = {}

    def add(
        alias: str,
        content: str,
        project: str | None = "alpha",
        *,
        subject: MemorySubject | None = None,
        valid_from: str | None = None,
        expires_at: str | None = None,
        supersedes: MemoryReference | None = None,
    ) -> None:
        records[alias] = store.add(
            content,
            project_identity(roots[project]) if project else None,
            MemorySource("add"),
            subject=subject,
            valid_from=valid_from,
            expires_at=expires_at,
            supersedes=supersedes,
        )

    add("test-rule", "代码修改后必须运行单元测试。")
    add("dev-tea", "开发同事王伟喜欢饮茶。", subject=MemorySubject("wang-dev", "王伟"))
    add("art-coffee", "设计同事王伟喜欢咖啡。", subject=MemorySubject("wang-art", "王伟"))
    add("language-old", "回答优先使用英语。", subject=MemorySubject("owner", "项目维护者"))
    old = records["language-old"]
    add(
        "language-new",
        "回答必须使用简体中文。",
        supersedes=MemoryReference(old.memory_id, old.revision),
    )
    add("expired-code", "临时连接口令是 OLD-CODE。", expires_at="2020-01-01T00:00:00Z")
    add("future-code", "未来发布代号 FUTURE-RELEASE。", valid_from="2100-01-01T00:00:00Z")
    add("alpha-private", "ALPHA 私有制品仓库地址为 alpha.example.test。")
    add("beta-private", "BETA 私有约定只在 BETA 项目使用。", "beta")
    add("shared-language", "跨项目沟通一律使用简体中文。", None)
    add("rolling", "发布策略使用滚动发布。", subject=MemorySubject("deployment", "发布系统"))
    add("blue-green", "发布策略使用蓝绿发布。", subject=MemorySubject("deployment", "发布系统"))
    # 替代操作追加了旧条目的结束版本，评估身份表也使用当前版本。
    records["language-old"] = store.history(old.memory_id, old.project)[-1]
    return store, roots, records


def evaluate(
    directory: Path,
    model_cache: Path,
    *,
    model: str = "BAAI/bge-small-zh-v1.5",
    backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    """在新目录执行相同十题的两策略对照，逐条保存实际选择后再输出汇总。

    默认使用已准备的本地模型，缺失仍保留生产降级事实，不将关键词结果冒充语义效果。
    backend 仅供工程测试注入；该模式始终标为 injected，不计算聊天 token 或费用。
    """
    directory.mkdir(parents=True, exist_ok=False)
    store, roots, records = seed_memories(directory)
    inverse = {record.memory_id: alias for alias, record in records.items()}
    encoder = backend if backend is not None else FastEmbedBackend(model_cache / "models", model)
    manifest = {
        "format": "memory_evaluation_v1",
        "backend": "injected" if backend is not None else "local_model",
        "requested_model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "max_entries": 3,
        "max_input_tokens": 6000,
        "semantic_min_score": 0.5,
        "cases": [asdict(case) for case in CASES],
        "sources": {alias: record.to_dict() for alias, record in records.items()},
    }
    write_record(directory / "manifest.json", manifest, exclusive=True)
    services = {
        (method, name): MemoryService(
            store,
            root,
            MemoryConfig(method=method, max_entries=3, max_input_tokens=6000),
            semantic=SemanticMemoryRanker(encoder, min_score=0.5) if method == "semantic" else None,
        )
        for method in ("keyword", "semantic")
        for name, root in roots.items()
    }
    rows = []
    for number, case in enumerate(CASES, 1):
        methods = ("keyword", "semantic") if number % 2 else ("semantic", "keyword")
        for method in methods:
            started = monotonic_ns()
            selected = services[method, case.project].select(case.query, 6000)
            elapsed = max(0, (monotonic_ns() - started) // 1000000)
            aliases = [inverse[record.memory_id] for record in selected.records]
            if any(
                record.to_dict() != records[alias].to_dict()
                for alias, record in zip(aliases, selected.records, strict=True)
            ):
                raise ValueError("实际选中版本与评估来源不一致。")
            scope = project_identity(roots[case.project])
            forbidden = {
                alias
                for alias, record in records.items()
                if record.project not in (None, scope) or not record.active(datetime.now(UTC))
            }
            if case.ambiguity:
                forbidden.update({"dev-tea", "art-coffee"})
            row = {
                "case_id": case.case_id,
                "method": method,
                "selected_aliases": aliases,
                "expected": list(case.expected),
                "matched": sorted(set(aliases) & set(case.expected)),
                "all_expected": bool(case.expected) and set(case.expected) <= set(aliases),
                "correct_empty": not case.expected
                and not aliases
                and selected.status == "no_match",
                "ambiguity_notice": "同名" in selected.warning,
                "forbidden_selected": sorted(set(aliases) & forbidden),
                "selection": selected.to_dict(),
                "elapsed_ms": elapsed,
            }
            write_record(directory / f"{number:02d}-{method}.json", row, exclusive=True)
            rows.append(row)
    groups = {
        method: _summarize([row for row in rows if row["method"] == method])
        for method in ("keyword", "semantic")
    }
    result = {
        "format": "memory_evaluation_v1",
        "backend": manifest["backend"],
        "requested_model": model,
        "groups": groups,
        "rows": rows,
        "limits": "固定合成记忆的检索对照；命中不代表回答正确。冲突题要求保留两份依据，不自动判定谁正确。模拟后端不是真实语义效果；降级次数单列。未请求聊天模型。",
    }
    write_record(directory / "report.json", result, exclusive=True)
    return result


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """保留所有题目作为分母；明确区分召回、空结果、来源违规和语义降级。"""
    positives = [row for row in rows if row["expected"]]
    negatives = [row for row in rows if not row["expected"]]
    expected_count = sum(len(row["expected"]) for row in positives)
    return {
        "positive_cases": len(positives),
        "all_expected_cases": sum(row["all_expected"] for row in positives),
        "recall_at_3": sum(len(row["matched"]) for row in positives) / expected_count,
        "empty_cases": len(negatives),
        "correct_empty_cases": sum(row["correct_empty"] for row in negatives),
        "forbidden_selected": sum(len(row["forbidden_selected"]) for row in rows),
        "fallback_cases": sum(
            row["selection"]["strategy"] == "keyword_fallback_v1" for row in rows
        ),
        "unavailable_cases": sum(row["selection"]["status"] == "unavailable" for row in rows),
        "elapsed_ms": sum(row["elapsed_ms"] for row in rows),
    }


def main(argv: list[str] | None = None) -> int:
    """显式选择新评估目录与已准备缓存；不加载聊天连接配置或下载模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.directory, args.model_cache, model=args.model)
    except (OSError, ValueError, AgentError):
        print("记忆评估未完成，请检查新目录和已保存的证据。", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "rows"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""固定二十工具的关键词/本地语义候选对照；不执行候选工具、不调用聊天 API。"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from typing import Any

from adam.config.settings import ToolDiscoveryConfig, ToolLimits
from adam.contracts.embedding import EmbeddingBackend
from adam.contracts.errors import AgentError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.contracts.tool_catalog import tool_schema_record
from adam.documents.embedding import FastEmbedBackend
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery_tools import ToolDiscovery, register_discovery
from adam.tool_calls.registry import ToolRegistry
from scripts.injection_capture import write_record

# 在运行前固定用途和真值；教学目录不冒充当前 Adam 已注册的全部工具。
CATALOG = (
    ("read_file", "files", "读取指定文本文件的逐行内容。", ToolCapability.READ_ONLY),
    (
        "search_text",
        "files",
        "在工作目录的文件正文中搜索文字，返回路径和行号。",
        ToolCapability.READ_ONLY,
    ),
    ("list_directory", "files", "列出指定目录下一级的文件和子目录名称。", ToolCapability.READ_ONLY),
    ("file_info", "files", "查看文件大小、修改时间和类型，不读取正文。", ToolCapability.READ_ONLY),
    ("git_status", "git", "查看仓库未提交修改和未跟踪文件的清单。", ToolCapability.READ_ONLY),
    (
        "git_diff",
        "git",
        "比较尚未提交的代码改动，显示修改前后的逐行差异。",
        ToolCapability.READ_ONLY,
    ),
    ("git_log", "git", "查询历史提交的作者、日期和提交说明。", ToolCapability.READ_ONLY),
    ("git_show", "git", "查看某一次指定历史提交的补丁和变更内容。", ToolCapability.READ_ONLY),
    ("list_sessions", "sessions", "列出保存的聊天会话名称、编号和时间。", ToolCapability.READ_ONLY),
    ("read_session", "sessions", "读取指定聊天会话的消息记录。", ToolCapability.READ_ONLY),
    ("list_memories", "memory", "列出当前项目保存的全部长期记忆条目。", ToolCapability.READ_ONLY),
    (
        "search_memories",
        "memory",
        "根据当前问题检索项目长期记忆与用户偏好。",
        ToolCapability.READ_ONLY,
    ),
    (
        "list_documents",
        "documents",
        "列出已建立索引的资料文件及来源版本。",
        ToolCapability.READ_ONLY,
    ),
    (
        "search_documents",
        "documents",
        "按问题检索资料库的相关原文片段和出处。",
        ToolCapability.READ_ONLY,
    ),
    (
        "create_file",
        "editing",
        "新建一个文本文件，已有同名文件时拒绝覆盖。",
        ToolCapability.EDIT_FILE,
    ),
    (
        "replace_text",
        "editing",
        "在现有文件中精确替换指定旧文本为新文本。",
        ToolCapability.EDIT_FILE,
    ),
    ("apply_patch", "editing", "按照统一补丁同时修改一个或多个文件。", ToolCapability.EDIT_FILE),
    ("delete_file", "editing", "删除指定文件，需要用户批准。", ToolCapability.EDIT_FILE),
    (
        "run_command",
        "process",
        "启动命令行程序并返回退出码和标准输出。",
        ToolCapability.RUN_PROCESS,
    ),
    ("git_branches", "git", "查看仓库的本地分支和当前所在分支。", ToolCapability.READ_ONLY),
)


@dataclass(frozen=True)
class QueryCase:
    """expected 是本题相关候选集合；空集合用于观察无工具需求和权限过滤的误召回。"""

    case_id: str
    query: str
    expected: tuple[str, ...] = ()
    mode: ExecutionMode = ExecutionMode.ACT


CASES = (
    QueryCase("exact", "git_log", ("git_log",)),
    QueryCase("file-body", "打开文件看看里面每一行写了什么", ("read_file",)),
    QueryCase("source-location", "在源码中定位配置加载函数出现的位置", ("search_text",)),
    QueryCase("directory", "当前目录有哪些文件和文件夹", ("list_directory",)),
    QueryCase("history", "查查以前是谁在什么时候提交了这些修改", ("git_log",)),
    QueryCase("working-change", "看看尚未提交的改动具体改了哪些行", ("git_diff",)),
    QueryCase("old-chat", "找出昨天那个聊天的消息内容", ("read_session",)),
    QueryCase("preference", "回忆用户以前对这个项目提出的偏好", ("search_memories",)),
    QueryCase("source-doc", "从资料库找到能回答这个问题的原文出处", ("search_documents",)),
    QueryCase("patch", "用手里的统一补丁修改多个源文件", ("apply_patch",)),
    QueryCase("no-tool", "解释希伯来字母"),
    QueryCase("plan-write", "replace_text", mode=ExecutionMode.PLAN),
)


class MeteredBackend:
    """只计编码调用与输入字符，不把字符数冒充 token，也不改变底层模型结果。"""

    def __init__(self, inner: EmbeddingBackend) -> None:
        self.inner = inner
        self.counts = dict.fromkeys(("passage_batches", "passage_texts", "query_calls", "chars"), 0)

    @property
    def identity(self) -> str:
        return self.inner.identity

    def passages(self, texts: list[str]) -> list[list[float]]:
        self.counts["passage_batches"] += 1
        self.counts["passage_texts"] += len(texts)
        self.counts["chars"] += sum(map(len, texts))
        return self.inner.passages(texts)

    def query(self, text: str) -> list[float]:
        self.counts["query_calls"] += 1
        self.counts["chars"] += len(text)
        return self.inner.query(text)


def catalog_registry() -> ToolRegistry:
    """建立可信的二十项教学目录；任何候选执行都直接报错，保证评估只测发现。"""
    specs = {
        name: schema(description, {"target": ("string", "明确的查询或操作目标")}, ["target"])
        for name, _, description, _ in CATALOG
    }
    return ToolRegistry(
        specs,
        dict.fromkeys(specs, _never_execute),
        ToolLimits(),
        capabilities={name: capability for name, _, _, capability in CATALOG},
        groups={name: group for name, group, _, _ in CATALOG},
    )


def evaluate(
    directory: Path,
    model_cache: Path,
    *,
    model: str = "BAAI/bge-small-zh-v1.5",
    backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    """在新目录先保存固定题目和目录，再保存每题实际页与汇总；已有目录拒绝覆盖。

    使用已缓存模型且禁止自动下载；注入编码器时明确标记 injected。
    两种策略各测同样十二题，top3 是候选页，不代表模型真正选择或执行成功。
    """
    directory.mkdir(parents=True, exist_ok=False)
    registry = catalog_registry()
    catalog = registry.directory()
    encoder = MeteredBackend(
        backend if backend is not None else FastEmbedBackend(model_cache / "models", model)
    )
    manifest = {
        "format": "tool_discovery_evaluation_v1",
        "created_at": datetime.now(UTC).isoformat(),
        "backend": "injected" if backend is not None else "local_model",
        "requested_model": model,
        "directory_version": catalog.version,
        "top_k": 3,
        "semantic_min_score": 0.5,
        "catalog": [asdict(entry) for entry in catalog.listings(ExecutionMode.ACT)],
        "schemas": catalog.snapshot([row[0] for row in CATALOG], ExecutionMode.ACT).definitions(),
        "cases": [asdict(case) for case in CASES],
    }
    write_record(directory / "manifest.json", manifest, exclusive=True)
    current_mode = ExecutionMode.ACT
    services = {
        method: register_discovery(
            registry,
            ToolDiscoveryConfig(ranking=method),
            lambda: current_mode,
            embedding_backend=encoder if method == "semantic" else None,
        )
        for method in ("keyword", "semantic")
    }
    rows = []
    for number, case in enumerate(CASES, 1):
        current_mode = case.mode
        # 交替运行先后顺序；模型首次加载计入第一道语义题，不混称为热查询耗时。
        methods = ("keyword", "semantic") if number % 2 else ("semantic", "keyword")
        for method in methods:
            tool_registry, service = services[method]
            turn_id = f"{number}-{method}"
            before = encoder.counts.copy()
            initial = service.definitions(turn_id)
            started = monotonic_ns()
            raw = tool_registry.execute("list_tools", json.dumps({"query": case.query, "limit": 3}))
            elapsed = max(0, (monotonic_ns() - started) // 1000000)
            page = json.loads(raw)
            if not page.get("ok"):
                raise ValueError("目录评估未得到完整候选页，停止汇总。")
            row = _result(case, method, service, turn_id, initial, page, elapsed)
            row["encoding"] = {key: value - before[key] for key, value in encoder.counts.items()}
            row["first_semantic_query"] = method == "semantic" and number == 1
            row["candidate_result_bytes"] = len(raw.encode("utf-8"))
            write_record(directory / f"{number:02d}-{method}.json", row, exclusive=True)
            rows.append(row)
    report = {
        "format": manifest["format"],
        "backend": manifest["backend"],
        "requested_model": model,
        "directory_version": catalog.version,
        "groups": {method: _summarize(rows, method) for method in ("keyword", "semantic")},
        "rows": rows,
        "limits": "固定教学目录和十二题，非聊天成功率；首题包括模型冷启动。Schema 与候选仅统计UTF-8字节，不能换算成精确token或费用。权限过滤成功不代表无关问题必定无需候选；未请求聊天模型，未执行候选工具。",
    }
    write_record(directory / "report.json", report, exclusive=True)
    return report


def _result(
    case: QueryCase,
    method: str,
    service: ToolDiscovery,
    turn_id: str,
    initial: list[dict[str, Any]],
    page: dict[str, Any],
    elapsed: int,
) -> dict[str, Any]:
    """原样保留候选页与权限真值；额外度量显式选择后下一请求的 Schema 大小。"""
    names = [entry["name"] for entry in page["tools"]]
    allowed = {entry.name for entry in service.directory.listings(case.mode)}
    if names:
        service.discover({"names": names}, None)
    return {
        "case_id": case.case_id,
        "method": method,
        "mode": case.mode.value,
        "query": case.query,
        "expected": list(case.expected),
        "selected": names,
        "matched": sorted(set(names) & set(case.expected)),
        "extra_candidates": sorted(set(names) - set(case.expected)),
        "forbidden_selected": sorted(set(names) - allowed),
        "strategy": page.get("ranking", {}).get("strategy", "keyword_v1"),
        "page": page,
        "elapsed_ms": elapsed,
        "initial_schema": tool_schema_record(initial),
        "selected_schema": tool_schema_record(service.definitions(turn_id)),
        "all_allowed_schema": tool_schema_record(
            service.directory.snapshot(allowed, case.mode).definitions()
        ),
    }


def _summarize(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    chosen = [row for row in rows if row["method"] == method]
    positives = [row for row in chosen if row["expected"]]
    return {
        "positive_cases": len(positives),
        "recall_at_3": sum(len(row["matched"]) for row in positives)
        / sum(len(row["expected"]) for row in positives),
        "all_expected_cases": sum(
            set(row["expected"]) <= set(row["selected"]) for row in positives
        ),
        "negative_cases": len(chosen) - len(positives),
        "correct_empty_cases": sum(not row["expected"] and not row["selected"] for row in chosen),
        "extra_candidates": sum(len(row["extra_candidates"]) for row in chosen),
        "forbidden_selected": sum(len(row["forbidden_selected"]) for row in chosen),
        "fallback_cases": sum(row["strategy"] == "keyword_fallback_v1" for row in chosen),
        "elapsed_ms": sum(row["elapsed_ms"] for row in chosen),
        "first_semantic_ms": next(
            (row["elapsed_ms"] for row in chosen if row["first_semantic_query"]), None
        ),
        "candidate_result_bytes": sum(row["candidate_result_bytes"] for row in chosen),
        "encoding": {
            key: sum(row["encoding"][key] for row in chosen) for key in chosen[0]["encoding"]
        },
    }


def _never_execute(_args: dict[str, Any], _record: ExecutionRecorder | None) -> dict[str, Any]:
    raise AssertionError("评估禁止执行候选工具。")


def main(argv: list[str] | None = None) -> int:
    """显式选择新的输出目录和已有本地缓存；缺失模型会在报告中记录降级。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.directory, args.model_cache, model=args.model)
    except (OSError, ValueError, AgentError):
        print("工具目录对照未完成，请检查新输出目录和已保存证据。", file=sys.stderr)
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

"""B42.3：固定读文件任务，对照主Agent处理与预算感知委派的真实本地流程。"""

import argparse
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from adam.application.subagents.manager import SubagentManager
from adam.bootstrap import build_application
from adam.config.loader import load_llm_config
from adam.config.roles import RoleProfile
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.cancellation import CancellationToken
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore

Factory = Callable[[str, str, str], httpx.MockTransport]


def compare_delegation(
    root: Path, llm: LLMConfig, *, live: bool = False, factory: Factory | None = None
) -> dict[str, Any]:
    """每个方案重建相同文件；不批准编辑/命令，汇总主子实际日志，未知成本不作零。"""
    if live == (factory is not None):
        raise ValueError("choose explicit live API or mock HTTP")
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in ("simple", "independent"):
        for arm in ("self", "budgeted"):
            trial = root / f"{case}-{arm}"
            row = _trial(trial, llm, case, arm, factory)
            rows.append(row)
            (trial / "result.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    report = {
        "mode": "live" if live else "mock_http",
        "rows": rows,
        "conclusion": "默认自己做简单任务；独立任务可有界委派。Mock只验证流程，真实质量/延迟收益待验收。",
    }
    (root / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _trial(
    root: Path, llm: LLMConfig, case: str, arm: str, factory: Factory | None
) -> dict[str, Any]:
    """新建隔离工作区，经真实spawn工具来源调用策略，结束后检查没有活子任务。"""
    workspace = root / "work"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    (workspace / "b.txt").write_text("B", encoding="utf-8")
    config = AppConfig(
        llm,
        root / "sessions",
        "读取指定文件，只返回要求的JSON。",
        agent=AgentConfig(max_session_tokens=50000),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    decisions = []

    def dispatch(manager: SubagentManager, name: str, task: str) -> dict[str, Any]:
        result = manager.dispatch_budgeted(
            ((name, task),),
            independent_tasks=1 if case == "simple" else 2,
            child_estimate=8000,
            main_estimate=8000,
        )
        decisions.append(result)
        return (
            {**result["spawned"][0], "decision": result["initial"]}
            if result["spawned"]
            else {"status": "not_dispatched", "decision": result["initial"]}
        )

    @contextmanager
    def child_client(cancel: CancellationToken) -> Iterator[LLMClient]:
        with httpx.Client(transport=factory(arm, case, "child") if factory else None) as http:
            yield LLMClient(llm, http)

    expected = {"a": "A"} if case == "simple" else {"a": "A", "b": "B"}
    prompt = (
        "读取a.txt，只返回JSON：a为文件内容。"
        if case == "simple"
        else "分别读取a.txt和b.txt，只返回JSON：a、b为对应文件内容。这两份读取任务独立。"
    )
    error, reply = None, ""
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=factory(arm, case, "parent") if factory else None) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(llm, http),
            workspace,
            child_client_factory=child_client if arm == "budgeted" else None,
            delegation_dispatch=dispatch if arm == "budgeted" else None,
            role=RoleProfile("executor", llm.model, frozenset({"read_file"}), 8)
            if arm == "self"
            else None,
        )
        try:
            reply = app.send(prompt)
        except Exception as failure:
            error = type(failure).__name__
        finally:
            app.close()
        usage = app.usage()
        manager = app._conversation.delegation
        terminal = manager.list_agents()["agents"] if isinstance(manager, SubagentManager) else []
    reports = [read_report(path) for path in config.sessions_dir.glob("*/*/*/*.jsonl")]
    reports.extend(
        read_report(path)
        for path in (config.sessions_dir / "_subagents").glob("*/sessions/*/*/*/*.jsonl")
    )
    requests = sum(len(turn["requests"]) for report in reports for turn in report["turns"])
    try:
        passed = error is None and json.loads(reply) == expected
    except ValueError:
        passed = False
    unchanged = (workspace / "a.txt").read_bytes() == b"A" and (
        workspace / "b.txt"
    ).read_bytes() == b"B"
    return {
        "arm": arm,
        "case": case,
        "passed": passed and unchanged,
        "error": error,
        "requests": requests,
        "known_tokens": usage.tokens,
        "total_tokens": None if usage.unknown else usage.tokens,
        "unknown_usage": usage.unknown,
        "decisions": decisions,
        "children": terminal,
        "all_children_terminal": all(child["status"] != "running" for child in terminal),
    }


def main() -> None:
    """手动真实API入口；没有--live时拒绝，不读取模型配置。"""
    parser = argparse.ArgumentParser(description="Compare bounded delegation and self execution")
    parser.add_argument("output", type=Path)
    parser.add_argument("--llm", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实API需要显式--live")
    report = compare_delegation(args.output, load_llm_config(args.llm), live=True)
    print(
        json.dumps(
            [
                {key: row[key] for key in ("arm", "case", "passed", "requests", "total_tokens")}
                for row in report["rows"]
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

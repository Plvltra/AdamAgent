"""B41.3：固定任务对照单 Agent+Skill 与两角色移交；真实 API 必须显式启用。"""

import argparse
import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from adam.application.facade import AgentApplication
from adam.application.handoff import RoleHandoff
from adam.bootstrap import build_application
from adam.config.loader import load_llm_config
from adam.config.roles import RoleProfile
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    ReflectionConfig,
    SkillsConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.errors import AgentError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from scripts.task_eval_assets import PreparedTask, prepare_task, verify_task

TransportFactory = Callable[[str, str, str], httpx.MockTransport]
GUIDANCE = "先读取相关文件，核对任务要求；只做必要修改，保留无关文件。已有正确实现不修改。"


def compare_roles(
    root: Path,
    llm: LLMConfig,
    *,
    live: bool = False,
    transport_factory: TransportFactory | None = None,
    limits: AgentConfig | None = None,
) -> dict[str, Any]:
    """独立重置两个固定任务、两个方案；总请求/token上限在一条移交链内共用。

    不批准命令，只批准试验工作区内文件编辑；保存失败和未知用量，不剔除失败样本。
    Mock只证明工程流程，不能据其token或成功率决定真实模型的角色收益。
    """
    if live == (transport_factory is not None):
        raise ValueError("choose explicit live API or injected MockTransport")
    root.mkdir(parents=True, exist_ok=False)
    skill = root / "skills" / "careful-edit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: careful-edit\ndescription: Inspect before making necessary code changes.\n---\n"
        + GUIDANCE,
        encoding="utf-8",
    )
    rows = []
    for case in ("repair", "counterexample"):
        for arm in ("single_skill", "handoff"):
            task = prepare_task(root / f"{case}-{arm}", case)
            row = _trial(
                task, arm, llm, limits or AgentConfig(), root / "skills", transport_factory
            )
            rows.append(row)
            (task.root / "result.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    summary = {}
    for arm in ("single_skill", "handoff"):
        selected = [row for row in rows if row["arm"] == arm]
        unknown = any(row["unknown_usage"] for row in selected)
        summary[arm] = {
            "passed": sum(row["passed"] for row in selected),
            "planned": len(selected),
            "requests": sum(row["requests"] for row in selected),
            "known_tokens": sum(row["known_tokens"] for row in selected),
            "total_tokens": None if unknown else sum(row["known_tokens"] for row in selected),
        }
    report = {
        "mode": "live" if live else "mock_http",
        "rows": rows,
        "summary": summary,
        "cost_unit": "token; no monetary price assumed",
        "conclusion": "仅验证对照流程，真实角色收益待验收。"
        if not live
        else "只针对两项固定任务比较成功数、请求与token；样本不足以推广角色优劣。",
    }
    (root / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _trial(
    task: PreparedTask,
    arm: str,
    llm: LLMConfig,
    limits: AgentConfig,
    skills: Path,
    factory: TransportFactory | None,
) -> dict[str, Any]:
    """用真实应用执行一组；调查消耗从执行者可用额度中扣除，不复制整段历史。"""
    apps = []
    reply, error = "", None
    with ExitStack() as stack:

        def open_stage(stage: str, remaining: AgentConfig) -> AgentApplication:
            profile = RoleProfile(
                "investigator" if stage == "investigator" else "executor",
                llm.model,
                frozenset(
                    {"read_file"} if stage == "investigator" else {"read_file", "replace_text"}
                ),
                remaining.max_model_requests,
            )
            config = AppConfig(
                llm,
                task.root / stage / "sessions",
                "按任务要求回答。" + GUIDANCE,
                agent=remaining,
                reflection=ReflectionConfig(enabled=False),
                tool_discovery=ToolDiscoveryConfig(mode="full"),
                skills=SkillsConfig(user_dir=skills),
            )
            store = stack.enter_context(SessionStore(config.sessions_dir))
            transport = factory(arm, task.case.case_id, stage) if factory else None
            http = stack.enter_context(httpx.Client(transport=transport))
            app = build_application(
                config,
                store,
                LLMClient(llm, http),
                task.workspace,
                role=profile,
                confirm_edit=lambda plan: plan.path.is_relative_to(task.workspace),
            )
            stack.callback(app.close)
            apps.append((app, store))
            return app

        try:
            if arm == "single_skill":
                app = open_stage("single", limits)
                listing = next(item for item in app.list_skills() if item.name == "careful-edit")
                reply = app.send(task.case.prompt, skill_id=listing.identifier)
            else:
                source = open_stage("investigator", limits)
                source.send("只调查以下任务并提出有文件依据的结论，不执行修改：" + task.case.prompt)
                used = source.usage()
                assert source.session is not None
                requests = _requests(apps[0][1], source.session.session_id)
                if (
                    used.unknown
                    or used.tokens >= limits.max_session_tokens
                    or requests >= limits.max_model_requests
                ):
                    raise AgentError("调查已耗尽共享额度或用量未知，未启动执行者。")
                target = open_stage(
                    "executor",
                    replace(
                        limits,
                        max_model_requests=limits.max_model_requests - requests,
                        max_session_tokens=limits.max_session_tokens - used.tokens,
                    ),
                )
                handoff = RoleHandoff(task.root / "handoffs")
                key = handoff.create(source, target, task.case.prompt, GUIDANCE)
                reply = handoff.execute(key, target)
        except Exception as failure:
            error = type(failure).__name__
        usages = [app.usage() for app, _ in apps]
        requests = sum(
            _requests(store, app.session.session_id) for app, store in apps if app.session
        )
        verification = verify_task(task, reply)
        return {
            "arm": arm,
            "case": task.case.case_id,
            "passed": error is None and verification["passed"],
            "verification": verification,
            "error": error,
            "requests": requests,
            "known_tokens": sum(usage.tokens for usage in usages),
            "unknown_usage": any(usage.unknown for usage in usages),
            "sessions": [app.session.session_id for app, _ in apps if app.session],
        }


def _requests(store: SessionStore, session_id: str) -> int:
    return sum(event["type"] == "request_started" for event in store.load_events(session_id))


def main() -> None:
    """手动真实模型对照入口；缺少--live时不读取密钥、不发起请求。"""
    parser = argparse.ArgumentParser(description="Compare role handoff and single agent + Skill")
    parser.add_argument("output", type=Path)
    parser.add_argument("--llm", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实API需要显式--live；离线验收通过compare_roles注入MockTransport")
    report = compare_roles(args.output, load_llm_config(args.llm), live=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

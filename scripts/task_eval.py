"""B31任务矩阵运行器；真实API必须显式启用，逐次保存结果，不把失败从分母删除。"""

import argparse
import math
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from adam.bootstrap import build_application
from adam.config.loader import load_config
from adam.config.settings import AppConfig
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from scripts.injection_capture import write_record
from scripts.task_eval_assets import CASES, PreparedTask, prepare_task, verify_task


def run_suite(
    root: Path,
    configs: dict[str, AppConfig],
    *,
    repeats: int = 1,
    cases: tuple[str, ...] | None = None,
    live: bool = False,
    transport_factory: Callable[[PreparedTask, str, int], httpx.MockTransport] | None = None,
    max_known_tokens: int = 100000,
    prices_cny_per_million: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """在独立目录运行配置×任务×重复次数；未知用量或达到总额度后明确记not_run。

    transport_factory仅用于离线HTTP工程验收；真实运行须live=True。不批准命令，
    文件编辑仅批准试验workspace内路径，不继承用户存储、扩展服务或额外目录权限。
    """
    if live == (transport_factory is not None):
        raise ValueError("choose explicit live API or injected mock HTTP")
    selected = cases if cases is not None else tuple(c.case_id for c in CASES)
    if (
        not configs
        or len(configs) > 8
        or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", n) for n in configs)
        or type(repeats) is not int
        or not 1 <= repeats <= 10
        or not selected
        or len(set(selected)) != len(selected)
        or set(selected) - {c.case_id for c in CASES}
        or type(max_known_tokens) is not int
        or max_known_tokens <= 0
    ):
        raise ValueError("invalid evaluation matrix or token limit")
    if prices_cny_per_million is not None and (
        len(prices_cny_per_million) != 2
        or any(
            type(p) not in (int, float) or not math.isfinite(p) or p < 0
            for p in prices_cny_per_million
        )
    ):
        raise ValueError("prices must be two finite nonnegative numbers")
    root.mkdir(parents=True, exist_ok=False)
    mode = "live_model" if live else "mock_http"
    matrix = [
        {
            "run_id": f"{name}-{case}-{iteration}",
            "config": name,
            "case": case,
            "iteration": iteration,
        }
        for name in configs
        for case in selected
        for iteration in range(repeats)
    ]
    write_record(
        root / "manifest.json",
        {
            "format": "adam_task_evaluation_v1",
            "mode": mode,
            "runs": matrix,
            "max_known_tokens": max_known_tokens,
            "prices_cny_per_million": prices_cny_per_million,
            "configs": {
                name: {
                    "model": c.llm.model,
                    "protocol": c.llm.protocol,
                    "agent": asdict(c.agent),
                    "memory": asdict(c.memory),
                    "planning": asdict(c.planning),
                    "reflection": asdict(c.reflection),
                    "context": asdict(c.context),
                    "tool_discovery": asdict(c.tool_discovery),
                }
                for name, c in configs.items()
            },
            "overrides": "isolated fixtures; fixed system prompt; no external commands/extensions; workspace-only edits",
        },
        exclusive=True,
    )
    results, spent, halt = [], 0, None
    summary: dict[str, Any] = {}
    for planned in matrix:
        location = root / planned["run_id"]
        if halt:
            result = {
                **planned,
                "status": "not_run",
                "passed": False,
                "reason": halt,
                "known_tokens": 0,
                "unknown_usage": False,
            }
        else:
            result = {
                **planned,
                **_run_trial(
                    location,
                    planned,
                    configs[planned["config"]],
                    transport_factory,
                    prices_cny_per_million,
                ),
            }
            spent += result["known_tokens"]
            if result["unknown_usage"] or result["status"] == "interrupted":
                halt = "unknown_usage_or_interrupted"
            elif spent >= max_known_tokens:
                halt = "known_token_limit"
        write_record(root / (planned["run_id"] + ".json"), result, exclusive=True)
        results.append(result)
        summary = {
            "mode": mode,
            "planned": len(matrix),
            "recorded": len(results),
            "attempted": sum(r["status"] != "not_run" for r in results),
            "passed": sum(r["passed"] for r in results),
            "success_rate": sum(r["passed"] for r in results) / len(matrix),
            "known_tokens": spent,
            "unknown_usage": any(r["unknown_usage"] for r in results),
            "quality_claim": "live fixed-task results"
            if live
            else "engineering only; not model quality",
            "runs": results,
        }
        write_record(root / "report.json", summary)
    return summary


def _run_trial(
    root: Path,
    planned: dict[str, Any],
    base: AppConfig,
    transport_factory: Callable[[PreparedTask, str, int], httpx.MockTransport] | None,
    prices: tuple[float, float] | None,
) -> dict[str, Any]:
    """每次重新组装真实应用，故障仍保留日志；费用只是显式单价的估算。"""
    started, reply, tools, identity = time.monotonic(), "", [], None
    error, task = None, None
    try:
        task = prepare_task(root, planned["case"])
        config = AppConfig(
            base.llm,
            task.sessions,
            "你是本地评估助手，只依据任务目录与已有记录完成任务。",
            agent=base.agent,
            context=base.context,
            memory=base.memory,
            planning=base.planning,
            reflection=base.reflection,
            tool_discovery=base.tool_discovery,
            memories_dir=task.memories,
        )
        transport = (
            transport_factory(task, planned["config"], planned["iteration"])
            if transport_factory
            else None
        )
        if transport_factory is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("offline evaluation requires MockTransport")
        with SessionStore(task.sessions) as store, httpx.Client(transport=transport) as http:
            app = build_application(
                config,
                store,
                LLMClient(config.llm, http),
                task.workspace,
                confirm_edit=lambda plan: plan.path.resolve().is_relative_to(
                    task.workspace.resolve()
                ),
                on_tool=tools.append,
            )
            try:
                reply = app.send(task.case.prompt)
            finally:
                identity = app.session.session_id if app.session else None
                app.close()
    except (Exception, KeyboardInterrupt) as failure:
        error = type(failure).__name__  # 不把HTTP正文、密钥或用户配置写入错误报告。
    check = (
        verify_task(task, reply, tuple(tools))
        if task
        else {"passed": False, "reasons": ["fixture unavailable"]}
    )
    input_tokens, output_tokens, unknown = 0, 0, False
    if task and identity:
        try:
            path = next(task.sessions.glob(f"*/*/*/{identity}.jsonl"))
            report = read_report(path)
            unknown = report["unknown_usage"]
            for turn in report["turns"]:
                for request in turn["requests"]:
                    usage = request["usage"]
                    if usage is None:
                        unknown = True
                    else:
                        input_tokens += usage["input_tokens"]
                        output_tokens += usage["output_tokens"]
        except Exception:
            unknown = True
    elif error:
        # 无完整会话轨迹无法证明远端消费为零，保守停止后续试验。
        unknown = True
    cost = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000 if prices else None
    return {
        "status": "interrupted"
        if error == "KeyboardInterrupt"
        else "failed"
        if error or not check["passed"]
        else "passed",
        "passed": error is None and check["passed"],
        "error": error,
        "verification": check,
        "reply": reply,
        "tools": tools,
        "session_id": identity,
        "duration_seconds": round(time.monotonic() - started, 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "known_tokens": input_tokens + output_tokens,
        "unknown_usage": unknown,
        "estimated_known_cost_cny": cost,
        "estimated_total_cost_cny": None if unknown else cost,
    }


def main(argv: list[str] | None = None) -> int:
    """显式--live才加载并请求配置中的模型；重复--config形成多配置矩阵。"""
    parser = argparse.ArgumentParser(description="固定任务真实模型评估，需显式启用付费请求")
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-known-tokens", type=int, default=100000)
    parser.add_argument("--price-cny", type=float, nargs=2, metavar=("INPUT_PER_M", "OUTPUT_PER_M"))
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("真实模型请求必须显式指定--live；本入口不会自动调用API")
    configs = {f"config{i}": load_config(path) for i, path in enumerate(args.config)}
    run_suite(
        args.output,
        configs,
        repeats=args.repeats,
        cases=tuple(args.case) if args.case else None,
        live=True,
        max_known_tokens=args.max_known_tokens,
        prices_cny_per_million=tuple(args.price_cny) if args.price_cny else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

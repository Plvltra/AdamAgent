"""显式比较通用摘要和当前问题导向摘要；复用生产规划，仅请求摘要，不执行历史任务。"""

import argparse
import json
import sys
from pathlib import Path
from time import monotonic_ns
from typing import Any

import httpx

from adam.config.loader import load_llm_config
from adam.config.settings import LLMConfig
from adam.context.compaction import CompactionService
from adam.contracts.errors import AgentError, LLMError, RequestInterrupted
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from scripts.compaction_cases import MAX_BYTES, CompactionSuite, load_suite
from scripts.compaction_fixture import (
    DEFAULT_CASES,
    FORMAT,
    LIMITS,
    digest,
    limits_record,
    schedule,
    seed_scenario,
    turn_identity,
)
from scripts.compaction_report import report_suite, review_template
from scripts.injection_capture import write_record


def prepare_rows(
    directory: Path, suite: CompactionSuite, runs: list[dict[str, Any]], client: LLMClient
) -> list[dict[str, Any]]:
    """先用生产规划准备所有输入；两组覆盖不一致时在请求之前失败。

    只导入专用合成历史，不执行 CompactionService.execute，也不激活评估摘要。
    当前问题 ID 只是评估输入标签，不创建未完成的用户轮次。
    """
    rows = []
    for number, scenario in enumerate(suite.scenarios):
        selected = [
            run
            for run in runs
            if next(c for c in suite.cases if c.case_id == run["case_id"]).scenario_id
            == scenario.scenario_id
        ]
        if not selected:
            continue
        with SessionStore(directory / "sources" / f"{number:02d}") as store:
            session = seed_scenario(store, scenario)
            service = CompactionService(store, client, LIMITS, lambda _: None)
            for item in selected:
                case = next(case for case in suite.cases if case.case_id == item["case_id"])
                focused = item["arm"] == "focused"
                plan = service.plan(
                    session,
                    current_turn=[{"role": "user", "content": case.query}] if focused else None,
                    turn_id=turn_identity(case.scenario_id, "query-" + case.case_id)
                    if focused
                    else None,
                )
                if plan is None:
                    raise ValueError("样本没有可压缩旧历史。")
                request = {
                    "messages": plan.messages,
                    "model": client.config.model,
                    "tools": [],
                    "max_tokens": LIMITS.summary_max_output_tokens,
                }
                rows.append(
                    {
                        **item,
                        "state": "planned",
                        "scenario_id": scenario.scenario_id,
                        "source_turn_ids": plan.source_turn_ids,
                        "estimated_input_tokens": plan.estimated_input_tokens,
                        "request": request,
                        "input_sha256": digest(request),
                        "summary": None,
                        "usage": None,
                        "elapsed_ms": None,
                        "error": None,
                    }
                )
    rows.sort(key=lambda row: row["index"])
    for left, right in zip(rows[::2], rows[1::2], strict=True):
        if left["source_turn_ids"] != right["source_turn_ids"]:
            raise ValueError("查询占用预算后两组覆盖不同，不能作为相同素材对照。")
    return rows


def run_suite(
    directory: Path,
    llm: LLMConfig,
    cases: Path,
    *,
    selected: tuple[str, ...] = DEFAULT_CASES,
    max_known_tokens: int = 50000,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """在全新目录中执行有界对照；未知消费、失败或取消停止后续请求，不重试。

    transport 仅供离线测试；报告按是否注入传输层标记 mock/live。
    输入估算加输出预留是本地防护，服务端实际返回量仍可能超过估算。
    """
    if type(max_known_tokens) is not int or not 1 <= max_known_tokens <= 1000000:
        raise ValueError("总 token 上限必须为 1～1000000。")
    with cases.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("样本超过 1 MiB。")
    directory.mkdir(parents=True, exist_ok=False)
    corpus = directory / "cases.json"
    with corpus.open("xb") as stream:
        stream.write(raw)
    suite = load_suite(corpus)
    runs = schedule(suite, selected)
    used = 0
    with httpx.Client(transport=transport, trust_env=False) as http:
        client = LLMClient(llm, http)
        rows = prepare_rows(directory, suite, runs, client)
        for row in rows:
            write_record(directory / f"{row['index']:02d}.json", row, exclusive=True)
        write_record(
            directory / "manifest.json",
            {
                "format": FORMAT,
                "backend": "mock" if transport else "live",
                "model": llm.model,
                "suite_sha256": suite.source_sha256,
                "selected": list(selected),
                "schedule": runs,
                "max_known_tokens": max_known_tokens,
                "limits": limits_record(),
                "input_hashes": {str(row["index"]): row["input_sha256"] for row in rows},
            },
            exclusive=True,
        )
        for row in rows:
            reserve = (
                row["estimated_input_tokens"]
                + LIMITS.safety_margin_tokens
                + LIMITS.summary_max_output_tokens
            )
            if used + reserve > max_known_tokens:
                break
            _request(client, directory / f"{row['index']:02d}.json", row)
            if row["usage"] is not None:
                used += sum(row["usage"])
            if row["state"] != "succeeded" or row["usage"] is None or used >= max_known_tokens:
                break
    return report_suite(directory)


def _request(client: LLMClient, path: Path, row: dict[str, Any]) -> None:
    """开始记录刷盘后才请求；保存失败保留未知边界，远端错误正文不进入报告。"""
    row["state"] = "started"
    write_record(path, row)
    started = monotonic_ns()
    try:
        result = client.complete(**row["request"])
        row["usage"] = list(result.usage) if result.usage is not None else None
        text = result.message.get("content")
        if (
            result.message.get("tool_calls")
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > MAX_BYTES
        ):
            row.update(state="failed", error="invalid_summary")
        else:
            row.update(
                summary=text.strip(),
                state="succeeded" if result.usage is not None else "failed",
                error=None if result.usage is not None else "usage_unknown",
            )
    except BaseException as error:
        usage = error.usage if isinstance(error, (LLMError, RequestInterrupted)) else None
        row.update(
            state="interrupted"
            if isinstance(error, (KeyboardInterrupt, RequestInterrupted))
            else "failed",
            usage=list(usage) if usage is not None else None,
            error=error.code.value if isinstance(error, AgentError) else "interrupted_or_local",
        )
        row["elapsed_ms"] = max(0, (monotonic_ns() - started) // 1000000)
        try:
            write_record(path, row)
        except OSError:
            error.add_note("评估结束记录保存失败；不要把该次消费视为零。")
        if isinstance(error, (KeyboardInterrupt, SystemExit, RequestInterrupted)):
            raise
        return
    row["elapsed_ms"] = max(0, (monotonic_ns() - started) // 1000000)
    write_record(path, row)


def main(argv: list[str] | None = None) -> int:
    """run 需显式付费开关；report 和 review-template 不加载连接配置或请求模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("directory", type=Path)
    run.add_argument("--llm-config", type=Path, required=True)
    run.add_argument("--allow-paid-api", action="store_true")
    run.add_argument(
        "--cases", type=Path, default=Path("examples/compaction-evaluation/cases-v1.json")
    )
    run.add_argument("--case", action="append", dest="selected")
    run.add_argument("--max-known-tokens", type=int, default=50000)
    report = commands.add_parser("report")
    report.add_argument("directory", type=Path)
    report.add_argument("--review", type=Path)
    template = commands.add_parser("review-template")
    template.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.action == "run" and not args.allow_paid_api:
        parser.error("run 会消费 API 额度，需显式提供 --allow-paid-api。")
    try:
        if args.action == "run":
            result = run_suite(
                args.directory,
                load_llm_config(args.llm_config),
                args.cases,
                selected=tuple(args.selected or DEFAULT_CASES),
                max_known_tokens=args.max_known_tokens,
            )
        elif args.action == "review-template":
            result = review_template(args.directory)
        else:
            result = report_suite(args.directory, review=args.review)
    except (ValueError, OSError, AgentError):
        print("压缩评估未完成；检查配置、目录和记录，已发生的消费不能当作零。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("压缩评估已中断；未自动重发，已有资料保留。", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

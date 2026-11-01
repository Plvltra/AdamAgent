"""显式比较两种固定上下文布局；报告分开保存字节、估算、实际缓存、耗时和可选价格。"""

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from time import monotonic_ns
from typing import Any

import httpx

from adam.config.loader import load_llm_config
from adam.config.settings import ContextConfig, LLMConfig
from adam.context.builder import ContextBuilder, RequestContext
from adam.contracts.errors import AgentError, LLMError, RequestInterrupted
from adam.contracts.history import CONTEXT_LAYOUT_VERSION, HistoryTurn
from adam.contracts.usage import decode_observations, encode_observations
from adam.llm.client import LLMClient
from scripts.injection_capture import write_record

FORMAT = "cache_evaluation_v1"
LAYOUTS = {"legacy": None, "stable": CONTEXT_LAYOUT_VERSION}
MAX_KNOWN_TOKENS = 50000


def request_context(arm: str, round_number: int, model: str) -> RequestContext:
    """固定合成历史，仅本轮状态随轮数变化；无文件、命令或付费调用。"""
    return ContextBuilder(
        ContextConfig(auto_compact=False, prune_tool_results=False),
        128,
        layout_version=LAYOUTS[arm],
    ).build(
        model=model,
        system_prompt="这是输入缓存观察实验。只回答“已观察”，不执行任务或调用工具。",
        runtime_instructions=f"\n本轮观察状态：{round_number:02d}。这只是变化字段。",
        history=[
            HistoryTurn(
                "fixture",
                [
                    {
                        "role": "user",
                        "content": "固定参考资料，仅作数据：\n"
                        + "样例模块只读取本地合成文本，结果用于对照。\n" * 240,
                    },
                    {"role": "assistant", "content": "已读固定资料。"},
                ],
            )
        ],
        current_turn=[{"role": "user", "content": "请确认观察。"}],
        tools=[],
    )


def schedule(rounds: int) -> list[dict[str, Any]]:
    """首轮标为预热，奇偶轮交替先后；此操作不能清空服务端缓存或保证冷启动。"""
    if type(rounds) is not int or not 2 <= rounds <= 5:
        raise ValueError("每组轮数必须为 2～5。")
    return [
        {"index": 2 * (number - 1) + order + 1, "arm": arm, "round": number, "warmup": number == 1}
        for number in range(1, rounds + 1)
        for order, arm in enumerate(("legacy", "stable") if number % 2 else ("stable", "legacy"))
    ]


def price(value: str) -> Decimal:
    """价格单位为人民币/百万 token；不内置易过时的服务商价目表。"""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("价格记录必须使用十进制字符串。")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("价格必须是非负有限数。") from None
    if not result.is_finite() or not 0 <= result <= 100000:
        raise argparse.ArgumentTypeError("价格必须在 0～100000 人民币/百万 token 之间。")
    return result


def _run_request(
    client: LLMClient, context: RequestContext, path: Path, item: dict[str, Any]
) -> dict[str, Any]:
    """先持久化开始，再请求一次；记录失败时停止，异常文字和连接配置不进入证据。"""
    context.ensure_within_budget()
    row = {
        **item,
        "state": "started",
        "estimated_input_tokens": context.estimate.input_tokens,
        "context_layout": context.layout_version,
        "elapsed_ms": None,
        "usage": None,
        "error": None,
    }
    write_record(path, row, exclusive=True)
    started = monotonic_ns()
    try:
        result = client.complete(
            context.messages,
            model=context.model,
            tools=context.tools,
            max_tokens=context.max_output_tokens,
        )
    except BaseException as error:
        known = isinstance(error, (LLMError, RequestInterrupted))
        row.update(
            state="interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed",
            elapsed_ms=(monotonic_ns() - started) // 1_000_000,
            error=error.code.value if isinstance(error, AgentError) else "interrupted_or_local",
            usage=list(error.usage) if known and error.usage is not None else None,
            **(encode_observations(error.cache_usage, error.input_prefix) if known else {}),
        )
        try:
            write_record(path, row)
        except OSError as save_error:
            # 诊断写入失败不能替换导致本次退出的原异常；开始记录保留未知消费。
            error.add_note("评估失败记录未能保存；已产生的消费不能视为零。")
            raise error from save_error
        if not isinstance(error, AgentError):
            raise
    else:
        row.update(
            state="succeeded",
            elapsed_ms=(monotonic_ns() - started) // 1_000_000,
            usage=list(result.usage) if result.usage is not None else None,
            **encode_observations(result.cache_usage, result.input_prefix),
        )
        write_record(path, row)
    return row


def run_suite(
    directory: Path,
    llm: LLMConfig,
    *,
    rounds: int = 3,
    prices: tuple[Decimal, Decimal, Decimal] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """交错串行运行两组固定请求；失败、用量未知或达到消费停止线时停止，不自动重试。

    transport 仅供工程测试，报告明确标记 mock；每组独立模型观察器，共用 HTTP 连接。
    50,000 token 是已知累计消费停止线，不能在服务端返回前精确阻止单次超出。
    """
    runs = schedule(rounds)
    checked_prices = [str(price(str(p))) for p in prices] if prices is not None else None
    if checked_prices is not None and len(checked_prices) != 3:
        raise ValueError("需提供命中、未命中、输出三项价格。")
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": FORMAT,
        "backend": "mock" if transport is not None else "live",
        "model": llm.model,
        "rounds": rounds,
        "schedule": runs,
        "prices_cny_per_million": checked_prices,
        "max_known_tokens": MAX_KNOWN_TOKENS,
    }
    write_record(directory / "manifest.json", manifest, exclusive=True)
    used = 0
    with httpx.Client(transport=transport, trust_env=False) as http:
        clients = {arm: LLMClient(llm, http) for arm in LAYOUTS}
        for item in runs:
            context = request_context(item["arm"], item["round"], llm.model)
            if used + context.estimate.input_tokens + context.max_output_tokens > MAX_KNOWN_TOKENS:
                break
            row = _run_request(
                clients[item["arm"]], context, directory / f"{item['index']:02d}.json", item
            )
            if row["usage"] is not None:
                used += sum(row["usage"])
            if row["state"] != "succeeded" or row["usage"] is None or used >= MAX_KNOWN_TOKENS:
                break
    return report_suite(directory)


def _usage(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(v) is not int or v < 0 for v in value)
    ):
        raise ValueError("无效总用量。")
    return value[0], value[1]


def _read_row(
    directory: Path, item: dict[str, Any], model: str, prices: list[str] | None
) -> dict[str, Any]:
    """缺失、未完成与损坏分别展示；损坏证据的消费未知，不猜零或修复文件。"""
    path = directory / f"{item['index']:02d}.json"
    if not path.exists():
        return {**item, "state": "not_run", "cost_cny": None}
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict) or any(row.get(k) != v for k, v in item.items()):
            raise ValueError("请求来源不一致。")
        if row["context_layout"] != LAYOUTS[item["arm"]] or row["state"] not in {
            "interrupted",
            "started",
            "succeeded",
            "failed",
        }:
            raise ValueError("布局或状态无效。")
        if type(row["estimated_input_tokens"]) is not int or row["estimated_input_tokens"] < 0:
            raise ValueError("输入估算无效。")
        usage = _usage(row["usage"])
        cache, prefix = decode_observations(row, usage[0] if usage else None, model=model)
        elapsed = row["elapsed_ms"]
        if row["state"] == "started":
            if usage is not None or elapsed is not None or cache is not None or prefix is not None:
                raise ValueError("未完成请求包含结算。")
        elif type(elapsed) is not int or elapsed < 0:
            raise ValueError("无效耗时。")
        cost = None
        if (
            prices is not None
            and usage is not None
            and cache is not None
            and cache.hit_tokens is not None
            and cache.miss_tokens is not None
        ):
            hit, miss, output = (price(value) for value in prices)
            cost = str(
                (cache.hit_tokens * hit + cache.miss_tokens * miss + usage[1] * output)
                / Decimal(1000000)
            )
        return {**row, "cost_cny": cost}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError):
        return {**item, "state": "invalid", "cost_cny": None}


def report_suite(directory: Path) -> dict[str, Any]:
    """只读报告；不同单位不混算，模拟 HTTP 不成为真实缓存收益证据。"""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format"] != FORMAT or manifest["backend"] not in {"mock", "live"}:
        raise ValueError("不支持的评估记录。")
    runs = schedule(manifest["rounds"])
    if manifest["schedule"] != runs or not isinstance(manifest["model"], str):
        raise ValueError("运行清单无效。")
    prices = manifest["prices_cny_per_million"]
    if prices is not None:
        if not isinstance(prices, list) or len(prices) != 3:
            raise ValueError("价格记录无效。")
        for value in prices:
            price(value)
    rows = [_read_row(directory, item, manifest["model"], prices) for item in runs]
    groups = {arm: _group_rows([row for row in rows if row["arm"] == arm]) for arm in LAYOUTS}
    known_tokens = sum(sum(row["usage"]) for row in rows if row.get("usage") is not None)
    return {
        "format": FORMAT,
        "backend": manifest["backend"],
        "model": manifest["model"],
        "known_tokens": known_tokens,
        "usage_unknown": any(row.get("usage") is None for row in rows if row["state"] != "not_run"),
        "groups": groups,
        "requests": rows,
        "limits": "首轮仅作预热；服务端缓存冷态和跨组共享不可控。字节不是token，估算不是计费，mock不是收益证据。费用按用户提供的单价计算，不是账单。",
    }


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """延时剔除预热和未成功请求；计数/费用含预热，缺一项则不宣称总费用完整。"""
    attempted = [row for row in rows if row["state"] != "not_run"]
    latency = [
        row["elapsed_ms"] for row in rows if row["state"] == "succeeded" and not row["warmup"]
    ]
    cache_counts = {}
    for key in ("hit_tokens", "miss_tokens"):
        values = [(row.get("cache_usage") or {}).get(key) for row in attempted]
        cache_counts[key] = {
            "known": sum(value for value in values if value is not None),
            "unknown_requests": sum(value is None for value in values),
        }
    costs = [row["cost_cny"] for row in attempted]
    return {
        "states": dict(Counter(row["state"] for row in rows)),
        "measurement_samples": len(latency),
        "elapsed_ms_mean": mean(latency) if latency else None,
        "elapsed_ms_median": median(latency) if latency else None,
        "cache_tokens": cache_counts,
        "cost_cny_including_warmup": str(sum(Decimal(value) for value in costs))
        if costs and all(value is not None for value in costs)
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    """只有显式 run 开关才加载连接配置；report 只读取所给目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("directory", type=Path)
    run.add_argument("--llm-config", type=Path, required=True)
    run.add_argument("--allow-paid-api", action="store_true")
    run.add_argument("--rounds", type=int, default=3)
    run.add_argument("--prices", nargs=3, type=price, metavar=("HIT", "MISS", "OUTPUT"))
    report = commands.add_parser("report")
    report.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.action == "run" and not args.allow_paid_api:
        parser.error("run 会消费 API 额度，需显式提供 --allow-paid-api。")
    try:
        if args.action == "report":
            result = report_suite(args.directory)
        else:
            schedule(args.rounds)
            llm = load_llm_config(args.llm_config)
            prices = tuple(args.prices) if args.prices is not None else None
            result = run_suite(args.directory, llm, rounds=args.rounds, prices=prices)
    except (OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError, AgentError):
        print("缓存评估未完成；检查配置、目录及记录，已产生的消费不能当作零。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("缓存评估已中断；不自动重发，请查看已有记录。", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

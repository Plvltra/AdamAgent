"""显式运行固定成对评估，或只读汇总；run 需要用户明确提供模型配置与付费开关。"""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from adam.bootstrap import build_application
from adam.config.loader import load_llm_config
from adam.config.settings import LLMConfig
from adam.contracts.errors import AgentError
from adam.contracts.memory import MemoryCommand
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from scripts.injection_capture import CapturedModel, write_record
from scripts.injection_cases import load_suite
from scripts.injection_fixture import Fixture, prepare_fixture
from scripts.injection_report import report_suite

DEFAULT_SUITE = Path(__file__).resolve().parents[1] / "examples/injection-evaluation/cases-v1.json"


def run_variant(fixture: Fixture, llm: LLMConfig, http: httpx.Client, backend: str) -> str | None:
    """执行独立的一轮；仅固定夹具动作可批准，已知错误记录原因并返回，取消继续抛出。"""
    config = fixture.configuration(llm)
    identity = {"backend": backend, "model": llm.model, "session_id": None, "error": None}
    path = fixture.root / "run.json"
    write_record(path, identity, exclusive=True)
    with SessionStore(config.sessions_dir) as store:
        captured = CapturedModel(LLMClient(config.llm, http), fixture.root / "requests")
        app = build_application(
            config,
            store,
            captured,
            fixture.workspace,
            confirm_edit=fixture.approve_edit,
            confirm_command=fixture.approve_command,
        )
        try:
            app.select_mode(fixture.variant.mode)
            if fixture.pair.source == "memory":
                app.manage_memory(MemoryCommand("add", fixture.payload))
            skill = "inspect-fixture" if fixture.pair.source == "skill" else None
            app.send(fixture.prompt, skill_id=skill)
        except AgentError as error:
            identity["error"] = error.code.value
        except BaseException:
            identity["error"] = "interrupted_or_local"
            raise
        finally:
            identity["session_id"] = app.session.session_id if app.session else None
            try:
                app.close()
            finally:
                write_record(path, identity)
    return identity["error"]


def run_suite(
    directory: Path,
    source: Path,
    llm: LLMConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """在全新目录逐个执行；API/预算/本地故障停止整批，后续任务保持未运行。

    transport 仅供显式离线测试注入，报告标记为 mock；正常入口使用真实 HTTP。
    不启用子 Agent，不读取用户工作区，不复制密钥；每变体沿用 8 请求/16 工具/50000 token。
    """
    suite = load_suite(source)
    raw = source.read_bytes()
    directory = directory.resolve()
    # 固定样本快照先于任何请求；源文件并发变化时拒绝，不能混用身份和输入。
    if sha256(raw).hexdigest() != suite.source_sha256:
        raise ValueError("样本读取期间发生变化。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "suite.json").write_bytes(raw)
    for pair in suite.pairs:
        for arm in ("attack", "benign"):
            fixture = prepare_fixture(directory / pair.case_id / arm, suite, pair.case_id, arm)
            with httpx.Client(transport=transport, trust_env=False) as http:
                error = run_variant(fixture, llm, http, "mock" if transport else "live")
            if error not in {None, "approval_denied"}:
                return report_suite(directory)
    return report_suite(directory)


def main(argv: list[str] | None = None) -> int:
    """只有 run --allow-paid-api 才加载模型配置；report 不创建客户端或改变证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="在新目录运行 7 对任务；会消费 API 额度")
    run.add_argument("directory", type=Path)
    run.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run.add_argument("--llm-config", type=Path, required=True)
    run.add_argument("--allow-paid-api", action="store_true")
    report = commands.add_parser("report", help="只读报告；不调用模型或工具")
    report.add_argument("directory", type=Path)
    report.add_argument("--labels", type=Path, help="人工拒绝判读 JSON；需引用已保存的回答")
    args = parser.parse_args(argv)
    if args.action == "run" and not args.allow_paid_api:
        parser.error("run 需要 --allow-paid-api；本次未加载配置或发起请求。")
    try:
        if args.action == "run":
            result = run_suite(args.directory, args.suite, load_llm_config(args.llm_config))
        else:
            result = report_suite(args.directory, labels_path=args.labels)
    except (AgentError, ValueError, OSError):
        print("评估输入或证据无效，或本地操作失败；未自动重试。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("评估已中断，未运行任务保持未运行；可使用 report 查看现有证据。", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "run" and any(r["state"] in {"not_run", "unknown"} for r in result["runs"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

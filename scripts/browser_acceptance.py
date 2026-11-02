"""C02.4 真实本地模型的文字/控件浏览器验收；不生成或读取截图。"""

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from adam.bootstrap import build_application
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    BrowserConfig,
    ContextConfig,
    LLMConfig,
    MemoryConfig,
    PlanningConfig,
    ReflectionConfig,
    RulesConfig,
    SkillsConfig,
    ToolLimits,
)
from adam.contracts.browser import BrowserApproval
from adam.contracts.errors import RequestInterrupted
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from scripts.browser_fixture import QUESTION, RECORD, SYSTEM, registry_site, write_json


def prepare(root: Path) -> None:
    """在全新目录冻结任务及观测指标；真实回答与站点副作用另行运行。"""
    root.mkdir(parents=True, exist_ok=False)
    write_json(
        root / "fixed.json",
        {
            "question": QUESTION,
            "system": SYSTEM,
            "record": RECORD,
            "expected_operator": "Eval User",
            "scoring": "Independent server file and HTTP activity verify actual task; final answer reviewed separately. No retries or task retuning after model failure. No screenshot/image observation.",
            "created_utc": datetime.now(UTC).isoformat(),
        },
    )


def configuration(root: Path, origin: str, port: int, model: str) -> AppConfig:
    """只连接回环模型及本批临时站点；不读取默认配置、密钥或真实浏览器资料。"""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Local model port must be 1024..65535")
    return AppConfig(
        LLMConfig(
            f"http://127.0.0.1:{port}/v1",
            model,
            "local-evaluation-not-a-secret",
            timeout_seconds=90,
            stream=True,
        ),
        root / "sessions",
        SYSTEM,
        agent=AgentConfig(
            max_model_requests=16,
            max_tool_calls=24,
            max_session_tokens=250000,
            max_output_tokens=1024,
        ),
        context=ContextConfig(
            max_input_tokens=14500, safety_margin_tokens=1024, auto_compact=False
        ),
        tool_limits=ToolLimits(max_result_chars=24000),
        memory=MemoryConfig(enabled=False),
        planning=PlanningConfig(enabled=False),
        reflection=ReflectionConfig(enabled=False),
        rules=RulesConfig(enabled=False),
        skills=SkillsConfig(enabled=False),
        browser=BrowserConfig(
            enabled=True,
            allowed_origins=(origin,),
            channel="chrome",
            max_chars=6000,
            max_elements=30,
        ),
    )


def execute(root: Path, origin: str, port: int, model: str) -> dict[str, Any]:
    """主应用自行选控件并逐次批准；失败及已知取消保留记录，已发动作不重发。"""
    config = configuration(root / "run", origin, port, model)
    workspace = root / "run/workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    bodies: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> None:
        bodies.append(json.loads(request.content))

    def approve(plan: BrowserApproval) -> bool:
        """只批准当前临时源的有界输入；不把页面文字当成权限。"""
        parts = urlsplit(plan.url)
        allowed = f"{parts.scheme}://{parts.netloc}" == origin and plan.operation in {
            "open",
            "click",
            "fill",
            "select",
            "scroll",
            "close",
        }
        if plan.value is not None and len(plan.value) > 100:
            allowed = False
        approvals.append({**asdict(plan), "approved": allowed})
        return allowed

    item: dict[str, Any] = {
        "question": QUESTION.replace("{origin}", origin),
        "started_utc": datetime.now(UTC).isoformat(),
        "cost": None,
        "currency": None,
        "cost_status": "unpriced_local_inference",
    }
    started = time.perf_counter_ns()
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(
            transport=InterruptibleTransport(),
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [capture]},
        ) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_browser=approve,
            on_tool=lambda name: print("tool " + name, flush=True),
        )
        try:
            try:
                item["answer"] = app.send(item["question"])
                item["turn_success"] = True
            except (Exception, RequestInterrupted) as error:
                item.update(
                    turn_success=False,
                    answer=getattr(error, "partial_reply", None),
                    error={"type": type(error).__name__, "message": str(error)},
                )
            session = app.session
            item["session_id"] = session.session_id if session else None
            write_json(
                root / "events.json", store.load_events(session.session_id) if session else []
            )
        finally:
            app.close()
    item.update(
        wall_duration_ms=(time.perf_counter_ns() - started) // 1000000,
        physical_model_requests=len(bodies),
        approvals=approvals,
        diagnostics=[read_report(p) for p in config.sessions_dir.rglob("*.jsonl")],
    )
    write_json(root / "model-bodies.json", bodies)
    write_json(root / "turn-result.json", item)
    return item


def observables(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """独立读取站点落盘文件；成功动作/最终文本不能代替实际查询、一次保存和回执。"""
    saved = json.loads((root / "site/receipts.json").read_text(encoding="utf-8"))
    expected = [{"id": "R0001", "record": "Orion", "code": "O-731", "operator": "Eval User"}]
    checks = {
        "queried_exact_record": any(q.strip().casefold() == "orion" for q in state["queries"]),
        "exactly_one_post": len(state["post_attempts"]) == 1,
        "saved_exact_expected_receipt": saved == expected,
        "read_actual_receipt": any(
            r["method"] == "GET" and r["path"] == "/receipt/R0001" for r in state["requests"]
        ),
    }
    return {
        "checks": checks,
        "actual_form_completion": all(checks.values()),
        "persisted_receipts": saved,
        "answer_quality": "requires_independent_semantic_review",
        "server_state": state,
    }


def run(root: Path, port: int, model: str, manifest: Path) -> None:
    """核对固定任务后执行一次；保留源版本、真实请求和失败，不根据结果换题重试。"""
    fixed = json.loads((root / "fixed.json").read_text(encoding="utf-8"))
    if fixed["question"] != QUESTION or fixed["system"] != SYSTEM or fixed["record"] != RECORD:
        raise ValueError("Frozen browser batch does not match this script")
    if (root / "run").exists():
        raise FileExistsError("Browser batch already ran")
    metadata = {
        "started_utc": datetime.now(UTC).isoformat(),
        "model": model,
        "local_port": port,
        "model_manifest": json.loads(manifest.read_text(encoding="utf-8-sig")),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "tracked_worktree_status": subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], text=True
        ).splitlines(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(
            Path(__file__).with_name("browser_fixture.py").read_bytes()
        ).hexdigest(),
        "scope": "Real local Qwen3 HTTP and production default keyword/on-demand browser tools, headless isolated Chrome and actual local HTTP. Text/DOM only; no images/screenshots. No default credentials, paid model or external application writes.",
        "timing_limit": "Exploratory n=1, concurrent full pytest may contend for CPU; not a performance benchmark.",
    }
    write_json(root / "runtime.json", metadata)
    with registry_site(root / "site") as (origin, state):
        metadata["fixture_origin"] = origin
        item = execute(root, origin, port, model)
    item["observable_task"] = observables(root, state)
    item["finished_utc"] = datetime.now(UTC).isoformat()
    write_json(root / "result.json", item)
    metadata["finished_utc"] = item["finished_utc"]
    write_json(root / "runtime.json", metadata)
    print(
        json.dumps(
            {
                "turn_success": item["turn_success"],
                "answer": item["answer"],
                "error": item.get("error"),
                "observable_task": item["observable_task"],
                "physical_model_requests": item["physical_model_requests"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


def main() -> int:
    """prepare 冻结任务，run 只连接显式本地模型和临时站点。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=8087)
    parser.add_argument("--model", default="adam-web-eval")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.directory)
    else:
        if args.manifest is None:
            parser.error("run requires --manifest")
        run(args.directory, args.port, args.model, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""在用户命令真正启动后取消或强杀宿主，验收 Linux 进程回收和会话恢复。"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx

from adam.bootstrap import build_application
from adam.config.settings import (
    AppConfig,
    CommandIsolationConfig,
    ContextConfig,
    LLMConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_execution import CommandIsolationResult
from adam.contracts.command_inputs import CommandInput
from adam.contracts.errors import RequestInterrupted
from adam.contracts.execution import ISOLATED_COMMAND_TOOL
from adam.isolation.commands import IsolatedCommands
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore

# 精确匹配独立 argv 项；bootstrap 的 JSON 参数和本探针的 JSON 查询都不会误匹配。
# PID 与 starttime 一起核对，只查询/清理本次已确认的用户命令及其隔离看守。
PROBE = """
import json, os, signal, sys, time
from pathlib import Path
query = json.loads(sys.argv[1])
def read(pid):
    try:
        fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
        args = Path('/proc/%s/cmdline' % pid).read_bytes().split(b'\\0')
        age = float(Path('/proc/uptime').read_text().split()[0]) - int(fields[19]) / os.sysconf('SC_CLK_TCK')
        return {'pid': int(pid), 'parent': int(fields[1]), 'start': int(fields[19]),
                'state': fields[0], 'age_seconds': round(age, 3), 'args': args}
    except (OSError, ValueError, IndexError):
        return None
selected = {}
if 'marker' in query:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():
                continue
            item = read(path.name)
            if not item or query['marker'].encode() not in item['args']:
                continue
            item['role'] = 'worker'
            selected[item['pid']] = item
            parent = read(item['parent'])
            for _ in range(8):
                if not parent or parent['pid'] <= 1:
                    break
                if any(b'from adam.isolation.linux_command import main' in arg for arg in parent['args']):
                    parent['role'] = 'supervisor'
                    selected[parent['pid']] = parent
                    break
                if any(arg.rsplit(b'/', 1)[-1] == b'bwrap' for arg in parent['args']):
                    parent['role'] = 'bubblewrap'
                    selected[parent['pid']] = parent
                parent = read(parent['parent'])
        if selected:
            break
        time.sleep(0.02)
else:
    for known in query['identities']:
        item = read(known['pid'])
        if item and item['start'] == known['start']:
            selected[item['pid']] = item
    if query.get('cleanup'):
        for item in selected.values():
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
print(json.dumps([{k: v for k, v in item.items() if k != 'args'} for item in selected.values()]))
"""


def probe(distribution: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    """读取本次进程身份；异常兜底清理不参与通过判定，也不按进程名批量杀死。"""
    value = subprocess.run(
        [
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wsl.exe"),
            "--distribution",
            distribution,
            "--exec",
            "/usr/bin/python3",
            "-I",
            "-c",
            PROBE,
            json.dumps(query),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=25,
        creationflags=0x08000000,
    )
    return json.loads(value.stdout)


def run_worker(root: Path, distribution: str) -> None:
    """生产应用和真实后端运行于独立进程；仅注入本测试的取消令牌，模型 HTTP 为替身。"""
    marker = (root / "marker").read_text(encoding="utf-8")
    config = AppConfig(
        LLMConfig("https://unused.test", "offline-fixture", "unused", stream=False),
        root / "sessions",
        "",
        context=ContextConfig(auto_compact=False),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
        command_isolation=CommandIsolationConfig(True, distribution),
    )
    token, done = CancellationToken(), Event()
    original = IsolatedCommands.run

    def controlled(
        self: IsolatedCommands,
        argv: list[str] | tuple[str, ...],
        *,
        timeout_seconds: int = 5,
        inputs: tuple[CommandInput, ...] = (),
        cancel: CancellationToken | None = None,
    ) -> CommandIsolationResult:
        return original(self, argv, timeout_seconds=timeout_seconds, inputs=inputs, cancel=token)

    def watch() -> None:
        while not done.wait(0.01):
            if (root / "cancel").exists():
                token.cancel()
                return

    IsolatedCommands.run = controlled
    thread = Thread(target=watch, daemon=True)
    thread.start()
    requests = 0
    outcome: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert requests == 1, "停止之后不应再次请求模型"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "command",
                                    "type": "function",
                                    "function": {
                                        "name": ISOLATED_COMMAND_TOOL,
                                        "arguments": json.dumps(
                                            {
                                                "argv": [
                                                    "python3",
                                                    "-I",
                                                    "-B",
                                                    "-c",
                                                    "import time; time.sleep(30)",
                                                    marker,
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    try:
        with (
            SessionStore(config.sessions_dir) as store,
            httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ):
            sid = store.create_session("real-command-fault", "")
            store.select_session(sid)
            store.append_turn(sid, "old question", "old answer")
            (root / "session_id").write_text(sid, encoding="utf-8")
            app = build_application(
                config, store, LLMClient(config.llm, http), root, confirm_command=lambda _: True
            )
            try:
                app.resume(sid)
                app.send("task")
                raise AssertionError("故障场景不应成功完成")
            except RequestInterrupted:
                outcome["error"] = "interrupted"
            finally:
                app.close()
        outcome["requests"] = requests
        (root / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    finally:
        done.set()
        thread.join(timeout=2)
        IsolatedCommands.run = original


def verify_case(root: Path, phase: str, distribution: str) -> dict[str, Any]:
    """确认真正用户进程及祖先后注入故障；全部回收后，持锁恢复且不能自动执行。"""
    root.mkdir()
    marker = "adam-command-" + secrets.token_hex(16)
    (root / "marker").write_text(marker, encoding="utf-8")
    identities: list[dict[str, Any]] = []
    cleared = False
    with (root / "host.log").open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.isolated_command_fault_acceptance",
                "--worker",
                str(root),
                "--distribution",
                distribution,
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
        )
        try:
            identities = probe(distribution, {"marker": marker})
            roles = {item.get("role") for item in identities}
            assert {"worker", "bubblewrap", "supervisor"} <= roles, identities
            worker = next(item for item in identities if item["role"] == "worker")
            assert worker["age_seconds"] < 1.5 and process.poll() is None, identities
            started = time.monotonic()
            if phase == "kill":
                process.kill()
            else:
                (root / "cancel").write_text("cancel", encoding="utf-8")
            process.wait(timeout=10)
            assert phase == "kill" or process.returncode == 0, (root / "host.log").read_text(
                encoding="utf-8", errors="replace"
            )
            deadline = time.monotonic() + 8
            while probe(distribution, {"identities": identities}):
                if time.monotonic() >= deadline:
                    raise AssertionError("测试隔离进程未全部回收")
                time.sleep(0.05)
            elapsed = time.monotonic() - started
            # 用户命令的墙钟上限为5秒；必须明显早于它，不能把自然超时算作取消清理。
            assert elapsed < 2.5, {"stop_seconds": elapsed, "identities": identities}
            cleared = True
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if identities and not cleared:
                probe(distribution, {"identities": identities, "cleanup": True})

    sid = (root / "session_id").read_text(encoding="utf-8")
    with SessionStore(root / "sessions") as store:
        store.select_session(sid)
        events = store.load_events(sid)
        turn = next(e["payload"] for e in reversed(events) if e["type"] == "turn_started")
        assert turn["user_text"] == "task"
        report = store.execution_report(sid, turn["turn_id"])
        records = store.load_executions(sid)
        assert len(records) == 1 and records[0]["environment"] == "isolated"
        assert records[0].get("exit_code") is None
        assert report.status == "interrupted"
        requests = sum(e["type"] == "request_started" for e in events)
        assert requests == 1 and store.load_messages(sid)[-1].content == "old answer"
        assert store.usage(sid) == (12, False)
        if phase == "kill":
            assert report.reason == "restart_recovery" and records[0]["status"] == "unknown"
            assert any(e["type"] == "session_recovered" for e in events)
            assert not (root / "outcome.json").exists()
        else:
            assert report.reason == "interrupted" and records[0]["status"] == "interrupted"
            assert records[0]["truncated"] is True
            outcome = json.loads((root / "outcome.json").read_text(encoding="utf-8"))
            assert outcome == {"error": "interrupted", "requests": 1}
        frozen = store.load_events(sid)
    with SessionStore(root / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == frozen
    return {
        "phase": phase,
        "requests": requests,
        "turn_status": report.status,
        "reason": report.reason,
        "command_status": records[0]["status"],
        "command_exit_code": None,
        "model_usage": {"tokens": 12, "unknown": False},
        "tracked_processes": identities,
        "all_reaped": cleared,
        "stop_and_reap_seconds": round(elapsed, 3),
        "restart_idempotent": True,
    }


def main() -> None:
    """显式真实验收；失败不生成通过报告，不安装后端，不调用付费模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        run_worker(args.worker, args.distribution)
        return
    if not args.output or args.output.exists():
        parser.error("--output must be a new report path")
    with tempfile.TemporaryDirectory(prefix="adam-command-fault-") as directory:
        root = Path(directory).resolve()
        results = []
        for phase in ("cancel", "kill"):
            results.append(verify_case(root / phase, phase, args.distribution))
            print(f"{phase}: passed", flush=True)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "distribution": args.distribution,
        "real_backend": True,
        "model_transport": "httpx.MockTransport",
        "paid_requests": 0,
        "cases": results,
        "limits": "取消用文件信号注入原 CancellationToken；强杀为 Windows 宿主进程。仅通过前记录本次 Linux 身份全部消失，不模拟按键，不证明其他 OS。",
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()

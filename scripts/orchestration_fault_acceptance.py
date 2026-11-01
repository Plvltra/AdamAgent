"""实际 WSL 故障与强杀验收；模拟模型 HTTP，临时会话、原工具和进程都真实运行。"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Timer
from typing import Any

import httpx

from adam.bootstrap import build_application
from adam.config.settings import (
    AppConfig,
    ContextConfig,
    LLMConfig,
    OrchestrationConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError, RequestInterrupted
from adam.contracts.orchestration import OrchestrationResult, ToolCallback
from adam.isolation.rpc import IsolatedOrchestrator
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore

PHASES = ("denied_path", "budget", "worker_exit", "malformed", "cancel", "kill")

# 只输出本次代码精确匹配的进程身份，不收集环境或其他进程的命令。starttime 防止 PID 复用。
PROBE = """
import json, os, signal, sys
from pathlib import Path
query = json.loads(sys.argv[1])
def read(pid):
    try:
        fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
        args = Path('/proc/%s/cmdline' % pid).read_bytes().split(b'\\0')
        return {'pid': int(pid), 'parent': int(fields[1]), 'start': int(fields[19]),
                'state': fields[0], 'args': args}
    except (OSError, ValueError, IndexError):
        return None
selected = {}
if 'encoded' in query:
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        item = read(path.name)
        if item and query['encoded'].encode() in item['args']:
            selected[item['pid']] = item
            parent = read(item['parent'])
            for _ in range(8):
                if not parent or parent['pid'] <= 1:
                    break
                if any(b'from adam.isolation.linux_rpc import main' in arg for arg in parent['args']):
                    parent['supervisor'] = True
                    selected[parent['pid']] = parent
                    break
                parent = read(parent['parent'])
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
    """读取测试进程的 Linux 身份；异常清理也只针对同 PID、同启动时间的已识别进程。"""
    result = subprocess.run(
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
        timeout=15,
        creationflags=0x08000000,
    )
    return json.loads(result.stdout)


def code_for(phase: str, identity: str) -> str:
    """固定故障程序，无用户输入拼接；每例先通过原工具，便于同步观测真实 worker。"""
    path = "../private.txt" if phase == "denied_path" else "notes.txt"
    prefix = f"value = call_tool('read_file', {{'path': {path!r}}})\n"
    suffix = {
        "denied_path": "result = {'denied': value['ok'] is False}",
        "budget": "result = call_tool('file_info', {'path': 'notes.txt'})",
        "worker_exit": "raise SystemExit(7)",
        "malformed": "import sys\nsys.__stdout__.write('not-json\\n')\nsys.__stdout__.flush()",
        "cancel": "import time\ntime.sleep(30)\nresult = 'should not finish'",
        "kill": "result = 'host should not release this callback'",
    }
    return f"# acceptance-{identity}\n" + prefix + suffix[phase]


def wait_file(path: Path, process: subprocess.Popen[bytes] | None = None) -> None:
    """同步真实执行边界；超时或子进程提前退出时报错，不把未到达的故障点算通过。"""
    deadline = time.monotonic() + 35
    while not path.exists():
        if (process is not None and process.poll() is not None) or time.monotonic() > deadline:
            raise RuntimeError(f"未到达验收边界：{path.name}")
        time.sleep(0.05)


def run_worker(root: Path, phase: str, distribution: str) -> None:
    """独立宿主进程运行生产应用；仅在一次真实底层调用结束后暂停，供父进程观察或强杀。"""
    identity = (root / "identity").read_text(encoding="utf-8")
    config = AppConfig(
        LLMConfig("https://unused.test", "offline-fixture", "unused", stream=False),
        root / "sessions",
        "",
        context=ContextConfig(auto_compact=False),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
        orchestration=OrchestrationConfig(True, distribution),
    )
    config = replace(config, agent=replace(config.agent, max_tool_calls=2))
    token, timers = CancellationToken(), []
    original = IsolatedOrchestrator.run

    def controlled(
        runner: IsolatedOrchestrator,
        code: str,
        invoke: ToolCallback,
        *,
        cancel: CancellationToken | None = None,
    ) -> OrchestrationResult:
        calls = 0

        def callback(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            value = invoke(name, arguments)
            calls += 1
            if calls == 1:
                (root / "ready").write_text("ready", encoding="utf-8")
                wait_file(root / "release")
                if phase == "cancel":
                    timer = Timer(0.3, token.cancel)
                    timers.append(timer)
                    timer.start()
            return value

        return original(runner, code, callback, cancel=token)

    IsolatedOrchestrator.run = controlled
    requests = 0
    outcome: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "code",
                        "type": "function",
                        "function": {
                            "name": "execute_code",
                            "arguments": json.dumps({"code": code_for(phase, identity)}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert requests == 2 and phase in {"denied_path", "worker_exit"}
            body = json.loads(request.content)
            value = json.loads(body["messages"][-1]["content"])
            if phase == "denied_path":
                assert value["ok"] and value["result"] == {"denied": True}
                assert "private sentinel" not in json.dumps(body)
            else:
                assert not value["ok"] and value["exit_code"] == 7 and not value["has_result"]
            outcome["tool_result"] = value
            message = {"role": "assistant", "content": "observed failure"}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    try:
        with (
            SessionStore(config.sessions_dir) as store,
            httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ):
            sid = store.create_session("real-rpc-fault", "")
            store.select_session(sid)
            store.append_turn(sid, "old question", "old answer")
            (root / "session_id").write_text(sid, encoding="utf-8")
            app = build_application(config, store, LLMClient(config.llm, http), root)
            try:
                app.resume(sid)
                outcome["reply"] = app.send("task")
            except RequestInterrupted:
                outcome["error"] = "interrupted"
            except AgentError as error:
                outcome["error"] = error.code.value
            finally:
                app.close()
        outcome["requests"] = requests
        (root / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    finally:
        IsolatedOrchestrator.run = original
        for timer in timers:
            timer.cancel()
            timer.join()


def verify_case(root: Path, phase: str, distribution: str) -> dict[str, Any]:
    """先确认真实 Linux 进程身份，再放行或强杀；退出后验证清理及持锁恢复，不自动重发。"""
    root.mkdir()
    identity = secrets.token_hex(16)
    (root / "identity").write_text(identity, encoding="utf-8")
    (root / "notes.txt").write_text("local evidence", encoding="utf-8")
    identities: list[dict[str, Any]] = []
    with (root / "host.log").open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.orchestration_fault_acceptance",
                "--worker",
                str(root),
                "--phase",
                phase,
                "--distribution",
                distribution,
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
        )
        cleared = False
        try:
            wait_file(root / "ready", process)
            encoded = base64.b64encode(code_for(phase, identity).encode()).decode("ascii")
            identities = probe(distribution, {"encoded": encoded})
            assert len(identities) >= 2 and any(i.get("supervisor") for i in identities)
            started = time.monotonic()
            if phase == "kill":
                process.kill()
            else:
                (root / "release").write_text("continue", encoding="utf-8")
            process.wait(timeout=15)
            assert phase == "kill" or process.returncode == 0, (root / "host.log").read_text(
                encoding="utf-8"
            )
            deadline = time.monotonic() + 10
            while probe(distribution, {"identities": identities}):
                if time.monotonic() >= deadline:
                    raise AssertionError("本次隔离进程尚未回收")
                time.sleep(0.1)
            cleared = True
            elapsed = time.monotonic() - started
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
        turn_id = turn["turn_id"]
        report = store.execution_report(sid, turn_id)
        starts = [e["payload"] for e in events if e["type"] == "tool_started"]
        assert len(starts) == 2 and starts[1]["parent_tool_call_id"] == "code"
        assert starts[0]["request_id"] == starts[1]["request_id"]
        request_count = sum(e["type"] == "request_started" for e in events)
        if phase == "kill":
            assert report.status == "interrupted" and report.reason == "restart_recovery"
            assert any(e["type"] == "session_recovered" for e in events)
            assert not (root / "outcome.json").exists()
        else:
            outcome = json.loads((root / "outcome.json").read_text(encoding="utf-8"))
            assert outcome["requests"] == request_count
            if phase in {"budget", "malformed", "cancel"}:
                expected = {
                    "budget": "tool_limit",
                    "malformed": "isolation_error",
                    "cancel": "interrupted",
                }
                assert outcome["error"] == expected[phase], outcome
                assert report.status == ("interrupted" if phase == "cancel" else "failed")
        if phase not in {"denied_path", "worker_exit"}:
            assert request_count == 1 and store.load_messages(sid)[-1].content == "old answer"
        else:
            assert request_count == 2 and report.status == "succeeded"
        frozen_events = store.load_events(sid)
    with SessionStore(root / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == frozen_events
    return {
        "phase": phase,
        "requests": request_count,
        "tool_starts": len(starts),
        "turn_status": report.status,
        "reason": report.reason,
        "tracked_linux_processes": len(identities),
        "all_reaped": cleared,
        "stop_and_reap_seconds": round(elapsed, 2),
        "restart_idempotent": True,
    }


def main() -> int:
    """显式验收使用真实 WSL；失败不生成通过报告，不安装后端或调用付费服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=PHASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not args.phase:
            parser.error("worker requires phase")
        run_worker(args.worker, args.phase, args.distribution)
        return 0
    if not args.output or args.output.exists():
        parser.error("--output must be a new report path")
    with tempfile.TemporaryDirectory(prefix="adam-rpc-fault-") as directory:
        root = Path(directory)
        private = root / "private.txt"
        private.write_text("private sentinel", encoding="utf-8")
        results = []
        for phase in PHASES:
            results.append(verify_case(root / phase, phase, args.distribution))
            print(f"{phase}: passed", flush=True)
        assert private.read_text(encoding="utf-8") == "private sentinel"
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "real_backend": True,
        "model_transport": "httpx.MockTransport",
        "paid_requests": 0,
        "cases": results,
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

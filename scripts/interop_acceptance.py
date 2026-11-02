"""用独立官方 SDK 服务验收 Adam 客户端；不把同实现自测当作互操作。"""

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from adam.interop.models import InteropError
from adam.interop.tasks import TaskClient

ROOT = Path(__file__).resolve().parents[1]


def terminal(client: TaskClient, local_id: str) -> dict[str, Any]:
    """有界轮询同一任务，返回终态；超时不重发。"""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = client.query(local_id)
        if task.state in {"completed", "failed", "canceled"}:
            return asdict(task)
        time.sleep(0.02)
    raise RuntimeError("reference task did not finish")


def evaluate(origin: str, root: Path) -> dict[str, Any]:
    """通过正式客户端覆盖五类边界，原始不符会直接失败并保留服务日志。"""
    client = TaskClient(origin)
    card = client.discover()
    client.submit("success", "hello reference")
    success = terminal(client, "success")
    assert success["state"] == "completed" and success["text"] == "HELLO REFERENCE"
    client.submit("failure", "fail")
    failure = terminal(client, "failure")
    assert failure["state"] == "failed"
    client.submit("cancel", "wait")
    canceled = client.cancel("cancel")
    cancellation = terminal(client, "cancel")
    assert cancellation["state"] == "canceled"
    try:
        client.submit("success", "hello reference")
    except InteropError:
        duplicate_blocked = True
    else:
        raise AssertionError("duplicate was sent")
    (root / "delay-query").write_text("delay one response", encoding="utf-8")
    client.timeout = 0.05
    try:
        client.query("success")
    except InteropError:
        unknown = asdict(client.tasks["success"])
    else:
        raise AssertionError("query did not time out")
    assert unknown["state"] == "unknown"
    client.timeout = 2
    recovered = terminal(client, "success")
    assert recovered["remote_id"] == success["remote_id"] and recovered["text"] == success["text"]
    # 再在协议层尝试向已终态 Task 发送消息，核对 SDK 拒绝重启而非产生新业务。
    with httpx.Client(trust_env=False) as http:
        repeated = http.post(
            origin,
            json={
                "jsonrpc": "2.0",
                "id": "repeat-terminal",
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": "repeat-terminal",
                        "taskId": success["remote_id"],
                        "contextId": success["context_id"],
                        "parts": [{"kind": "text", "text": "hello reference"}],
                    }
                },
            },
        ).json()
    assert "error" in repeated
    calls = [
        json.loads(line) for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [call["text"] for call in calls] == ["hello reference", "fail", "wait"]
    return {
        "card": asdict(card),
        "success": success,
        "failure": failure,
        "cancel_response": asdict(canceled),
        "cancellation": cancellation,
        "duplicate_blocked_locally": duplicate_blocked,
        "terminal_repeat_response": repeated,
        "query_timeout": unknown,
        "recovered": recovered,
        "actual_calls": calls,
        "passed": True,
    }


def main() -> None:
    """启动/关闭自有参考进程，报告只保存非敏感测试事实。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    with (root / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                str(args.reference_python.resolve()),
                str(ROOT / "scripts/support/a2a_reference.py"),
                str(root),
            ],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            deadline = time.monotonic() + 12
            while True:
                if process.poll() is not None:
                    raise RuntimeError("reference exited; inspect server.log")
                if time.monotonic() > deadline:
                    raise RuntimeError("reference readiness timeout")
                ready = root / "ready.json"
                if ready.exists():
                    identity = json.loads(ready.read_text(encoding="utf-8"))
                    try:
                        TaskClient(identity["origin"], timeout=0.2).discover()
                        break
                    except InteropError:
                        pass
                time.sleep(0.05)
            report = evaluate(identity["origin"], root)
            report["reference"] = identity
            (root / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        finally:
            process.communicate("stop\n", timeout=8)
    print(
        json.dumps(
            {
                "passed": True,
                "report": str(root / "report.json"),
                "process_exited": process.returncode,
            }
        )
    )


if __name__ == "__main__":
    main()

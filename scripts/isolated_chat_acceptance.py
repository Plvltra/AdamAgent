"""模拟模型 HTTP，验收生产聊天中的真实隔离命令、批准、日志与零执行重放。"""

import argparse
import hashlib
import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
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
from adam.contracts.errors import ApprovalDeniedError
from adam.contracts.execution import ISOLATED_COMMAND_TOOL
from adam.contracts.models import CommandRequest
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter
from adam.recording.runtime import CaptureRuntime, replay_turn
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore


def verify(distribution: str = "Ubuntu") -> dict[str, Any]:
    """只在临时根运行；隔离不可用就失败，不用宿主或 Mock 进程补过验收。"""
    with tempfile.TemporaryDirectory(prefix="adam-isolated-chat-") as directory:
        root = Path(directory)
        cases = (
            ("read", ["cat", "selected/note.txt"], True, 0),
            ("nonzero", ["false"], True, 1),
            ("readonly", ["python3", "-I", "-B", "-c", "open('/work/forbidden', 'w')"], True, 1),
            ("denied", ["true"], False, None),
        )
        results = [
            _case(root / name, distribution, argv, approved, expected)
            for name, argv, approved, expected in cases
        ]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "distribution": distribution,
        "real_backend": True,
        "model_transport": "httpx.MockTransport",
        "paid_requests": 0,
        "production_bootstrap": True,
        "cases": results,
        "limits": "验证生产工具集成；真实执行中强杀和 PID 清理由 B19.3 单独验收。",
    }


def _case(
    root: Path, distribution: str, argv: list[str], approved: bool, expected: int | None
) -> dict[str, Any]:
    """每个案例独立会话，核对实际批准对象、原文件、持久事实、额度与重放。"""
    root.mkdir()
    original = "实际字节\r\nsecond line\n".encode()
    target = root / "note.txt"
    target.write_bytes(original)
    version = hashlib.sha256(original).hexdigest()
    config = AppConfig(
        LLMConfig("https://unused.test", "offline-fixture", "unused", stream=False),
        root / "sessions",
        "",
        context=ContextConfig(auto_compact=False),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
        command_isolation=CommandIsolationConfig(True, distribution),
    )
    config = replace(config, agent=replace(config.agent, max_tool_calls=1))
    bodies: list[dict[str, Any]] = []
    approvals: list[CommandRequest] = []

    def confirm(command: CommandRequest) -> bool:
        approvals.append(command)
        assert command.environment == "isolated" and str(command.cwd) == "/work"
        assert command.argv == tuple(argv) and command.distribution == distribution
        assert command.input_files[0].version == version
        assert Path(command.input_files[0].source) == target.resolve()
        return approved

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "command",
                        "type": "function",
                        "function": {
                            "name": ISOLATED_COMMAND_TOOL,
                            "arguments": json.dumps(
                                {"argv": argv, "inputs": {"selected/note.txt": "note.txt"}}
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert approved and len(bodies) == 2
            result = json.loads(body["messages"][-1]["content"])
            assert result["status"] == "completed" and result["exit_code"] == expected
            assert result["environment"] == "isolated" and result["reason"] == "exited"
            if root.name == "read":
                assert result["stdout"].encode("utf-8") == original
            if root.name == "readonly":
                assert "Read-only file system" in result["stderr"]
            message = {"role": "assistant", "content": "actual outcome recorded"}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    capture = root / "capture.jsonl"
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        sid = store.create_session(root.name, "")
        store.select_session(sid)
        with RecordingWriter(capture, capture_seed(config, store, sid, "task", root)) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            runtime = CaptureRuntime(recorder)
            app = build_application(
                config,
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                root,
                confirm_command=confirm,
                decorate_tools=runtime.tools,
                observe_conversation=runtime.bind,
            )
            app.resume(sid)
            try:
                if approved:
                    assert recorder.run(lambda: app.send("task")) == "actual outcome recorded"
                else:
                    try:
                        recorder.run(lambda: app.send("task"))
                    except ApprovalDeniedError:
                        pass
                    else:
                        raise AssertionError("denial must stop the turn")
            finally:
                app.close()
        events = store.load_events(sid)
        record = store.load_executions(sid)[0]
        assert record["status"] == ("completed" if approved else "denied")
        assert record["exit_code"] == expected and record["environment"] == "isolated"
        assert record["input_files"][0]["version"] == version
        assert len(approvals) == 1 and target.read_bytes() == original
        if approved:
            requests = [e["payload"] for e in events if e["type"] == "request_started"]
            assert requests[-1]["context"]["runtime_state"]["tools"]["used"] == 1
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == events
        assert reopened.load_executions(sid)[0]["status"] == record["status"]
    target.unlink()
    replay = replay_turn(capture, root / "replay")
    assert replay["status"] == "matched"
    return {
        "name": root.name,
        "status": record["status"],
        "exit_code": expected,
        "reason": record.get("reason"),
        "input_version_verified": True,
        "host_source_unchanged": True,
        "restart_verified": True,
        "replay_after_source_removed": replay["status"],
        "http_requests": len(bodies),
    }


def main() -> int:
    """排他写出验收摘要；失败保留异常，不覆盖旧证据，也不调用付费模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--distribution", default="Ubuntu")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("验收报告已存在，请选择新的输出文件。")
    report = verify(args.distribution)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("真实隔离命令聊天、批准、日志恢复及重放通过；付费请求 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

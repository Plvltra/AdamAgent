"""模拟 HTTP 驱动生产聊天和真实隔离 worker；只使用临时文件，不调用模型 API。"""

import argparse
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
    ContextConfig,
    LLMConfig,
    OrchestrationConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter
from adam.recording.runtime import CaptureRuntime, replay_turn
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore

CODE = (
    "info = call_tool('file_info', {'path': 'notes.txt'})\n"
    "content = call_tool('read_file', {'path': 'notes.txt'})\n"
    "result = {'size': info['size'], 'selected': [row['text'] for row in content['lines'] if 'mm' in row['text']]}"
)


def verify(distribution: str = "Ubuntu") -> dict[str, Any]:
    """实际经过主应用组装、权限、额度、工具日志和重放；没有后端时失败，不用替身补过。"""
    with tempfile.TemporaryDirectory(prefix="adam-chat-rpc-") as directory:
        root = Path(directory)
        target = root / "notes.txt"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8", newline="\n")
        config = AppConfig(
            LLMConfig("https://unused.test", "offline-fixture", "unused", stream=False),
            root / "sessions",
            "",
            context=ContextConfig(auto_compact=False),
            reflection=ReflectionConfig(enabled=False),
            tool_discovery=ToolDiscoveryConfig(mode="full"),
            orchestration=OrchestrationConfig(True, distribution),
        )
        config = replace(config, agent=replace(config.agent, max_tool_calls=3))
        bodies = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if len(bodies) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "code",
                            "type": "function",
                            "function": {
                                "name": "execute_code",
                                "arguments": json.dumps({"code": CODE}),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                assert len(bodies) == 2
                value = json.loads(body["messages"][-1]["content"])
                assert value["ok"] and value["tool_calls"] == 2 and value["exit_code"] == 0
                assert value["result"] == {"size": 17, "selected": ["gamma"]}
                assert [item["name"] for item in value["nested_calls"]] == [
                    "file_info",
                    "read_file",
                ]
                assert len({item["tool_call_id"] for item in value["nested_calls"]}) == 2
                assert "alpha" not in json.dumps(body["messages"], ensure_ascii=False)
                message = {"role": "assistant", "content": "gamma"}
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
            sid = store.create_session("real-isolation", "")
            store.select_session(sid)
            with RecordingWriter(capture, capture_seed(config, store, sid, "task", root)) as writer:
                recorder = TurnRecorder(writer, lambda: store.load_events(sid))
                runtime = CaptureRuntime(recorder)
                app = build_application(
                    config,
                    store,
                    RecordingModel(LLMClient(config.llm, http), recorder),
                    root,
                    decorate_tools=runtime.tools,
                    observe_conversation=runtime.bind,
                )
                app.resume(sid)
                try:
                    assert recorder.run(lambda: app.send("task")) == "gamma"
                finally:
                    app.close()
            events = store.load_events(sid)
            starts = [e["payload"] for e in events if e["type"] == "tool_started"]
            assert [e["name"] for e in starts] == ["execute_code", "file_info", "read_file"]
            assert all(e["parent_tool_call_id"] == "code" for e in starts[1:])
            assert len({e["request_id"] for e in starts}) == 1
            requests = [e["payload"] for e in events if e["type"] == "request_started"]
            assert requests[-1]["context"]["runtime_state"]["tools"] == {
                "maximum": 3,
                "used": 3,
                "remaining": 0,
            }
        with SessionStore(config.sessions_dir) as reopened:
            assert reopened.load_events(sid) == events
            assert reopened.load_messages(sid)[-1].content == "gamma"
        target.unlink()
        replay = replay_turn(capture, root / "replay")
        assert replay["status"] == "matched" and replay["reply"] == "gamma"
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "real_backend": True,
            "model_transport": "httpx.MockTransport",
            "paid_requests": 0,
            "production_bootstrap": True,
            "worker_calls": 2,
            "shared_tool_count": 3,
            "nested_source_verified": True,
            "intermediate_text_in_model": False,
            "restart_verified": True,
            "replay_status": replay["status"],
            "reply": "gamma",
        }


def main() -> int:
    """显式运行后排他写出脱离临时路径的验收摘要；失败保留异常，不伪造通过。"""
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
    print("真实聊天编排、日志重启与离线重放通过；付费模型请求 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

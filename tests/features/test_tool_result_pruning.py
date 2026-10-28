"""真实文件工具结果在旧历史中缩减，HTTP 模型模拟，原始证据和诊断来源保留。"""

import json
from dataclasses import replace

import httpx

from adam.config import LLMConfig
from adam.conversation import Conversation
from adam.diagnostics import read_report
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.tool_calls import ToolLimits, ToolRegistry


def test_old_tool_result_is_pruned_in_request_only(tmp_path):
    (tmp_path / "large.txt").write_text(("中" * 60 + "\n") * 100, encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "done"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"large.txt"}'},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = Conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key"), http),
            "",
            ToolRegistry(tmp_path, limits=ToolLimits(max_result_chars=16000)),
        )
        conversation.send("read file")
        session_id = conversation.session.session_id
        for _ in range(2):
            store.append_turn(session_id, "recent question", "recent answer")
        original = store.load_messages(session_id)
        full = conversation.preview_context()
        conversation.context_builder.limits = replace(
            conversation.context_builder.limits,
            max_input_tokens=int(full.candidate_input_tokens / 0.85),
            safety_margin_tokens=0,
        )
        path = next(store.root.rglob("*.jsonl"))
        before = path.read_bytes()
        preview = conversation.preview_context()
        assert preview.pruned_tools and path.read_bytes() == before
        assert store.load_messages(session_id) == original
        conversation.send("next")
        tool_message = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
        body = json.loads(tool_message["content"])
        assert body["context_pruned"] and body["source"]["tool_call_id"] == "read1"
        context_record = read_report(path)["turns"][-1]["requests"][0]["context"]
        assert context_record["pruned_tools"] == list(preview.pruned_tools)
        assert path.read_bytes().startswith(before)
        assert len(requests) == 3  # 缩减过程没有额外模型请求。
        assert store.load_messages(session_id)[: len(original)] == original

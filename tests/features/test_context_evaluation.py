"""固定长对话的离线工程对比；模型按可见代号回应，不评估真实摘要质量。"""

import json

import httpx

from adam.config.settings import ContextConfig, LLMConfig
from adam.context.builder import estimate_json_tokens
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def test_fixed_long_history_comparison(tmp_path):
    results = {}
    for mode, limit, auto in [
        ("full", 100000, False),
        ("selection", 9000, False),
        ("summary", 9000, True),
    ]:
        requests = []

        def respond(request, requests=requests):
            body = json.loads(request.content)
            requests.append(body)
            visible = json.dumps(body["messages"], ensure_ascii=False)
            if "tools" not in body:
                assert "ORION" in visible  # 真正发出的摘要素材包含早期约束。
                text = "项目代号 ORION；后续回答须保留。"
            else:
                text = "ORION" if "ORION" in visible else "unknown"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
                    ],
                    "usage": {
                        "prompt_tokens": sum(
                            estimate_json_tokens(message) for message in body["messages"]
                        ),
                        "completion_tokens": 20,
                    },
                },
            )

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as http,
            SessionStore(tmp_path / mode) as store,
        ):
            conversation = build_conversation(
                store,
                LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
                "system",
                create_tool_registry(tmp_path),
                context_config=ContextConfig(
                    max_input_tokens=limit, safety_margin_tokens=0, auto_compact=auto
                ),
            )
            session_id = conversation.new().session_id
            store.append_turn(session_id, "项目代号是 ORION", "remember")
            for index in range(8):
                store.append_turn(session_id, f"noise {index}", "中" * 1000)
            answer = conversation.send("项目代号是什么")
            chat_request = requests[-1]
            results[mode] = {
                "answer": answer,
                "requests": len(requests),
                "chat_input_estimate": sum(
                    estimate_json_tokens(m) for m in chat_request["messages"]
                ),
                "known_tokens": store.usage(session_id)[0],
            }
    assert results["full"]["answer"] == results["summary"]["answer"] == "ORION"
    assert results["selection"]["answer"] == "unknown"
    assert results["summary"]["chat_input_estimate"] < results["full"]["chat_input_estimate"]
    assert results["summary"]["requests"] == 2 and results["full"]["requests"] == 1
    print(json.dumps(results, ensure_ascii=False))

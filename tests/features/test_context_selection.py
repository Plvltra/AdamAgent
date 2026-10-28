"""按完整轮次选择历史；模型使用假 HTTP，选择记录与原始日志实际落盘。"""

import json

import httpx
import pytest

from adam.config import ContextConfig, LLMConfig
from adam.diagnostics import read_report
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def test_selection_is_contiguous_persistent_and_traceable(tmp_path):
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    root = tmp_path / "sessions"
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, SessionStore(root) as store:
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key"), http),
            "system constraint",
            create_tool_registry(tmp_path),
            context_config=ContextConfig(
                max_input_tokens=8192,
                safety_margin_tokens=0,
                retain_recent_turns=0,
                auto_compact=False,
            ),
        )
        session_id = conversation.new().session_id
        store.append_turn(session_id, "early constraint", "small")
        store.append_turn(session_id, "middle noise", "中" * 12000)
        store.append_turn(session_id, "recent", "recent answer")
        turns = store.load_turns(session_id)
        path = next(root.rglob("*.jsonl"))
        original = path.read_bytes()
        preview = conversation.preview_context()
        assert preview.selected_turn_ids == (turns[2].turn_id,)
        assert preview.omitted_turn_ids == (turns[0].turn_id, turns[1].turn_id)
        assert path.read_bytes() == original
        conversation.send("next")
        assert payloads[0]["messages"] == preview.messages + [{"role": "user", "content": "next"}]
        assert path.read_bytes().startswith(original)
        report = read_report(path)
        selection = report["turns"][-1]["requests"][0]["context"]
        assert selection["selected_turn_ids"] == [turns[2].turn_id]
        assert selection["omitted_turn_ids"] == [turn.turn_id for turn in turns[:2]]
        expected = conversation.preview_context()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP"))
        ) as http,
        SessionStore(root) as store,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key"), http),
            "changed default",
            create_tool_registry(tmp_path),
            context_config=ContextConfig(
                max_input_tokens=8192,
                safety_margin_tokens=0,
                retain_recent_turns=0,
                auto_compact=False,
            ),
        )
        conversation.resume(session_id)
        assert conversation.preview_context() == expected
        assert len(store.load_turns(session_id)) == 4


def test_invalid_selection_never_enters_log(tmp_path):
    with SessionStore(tmp_path) as store:
        session = store.create_session("A", "")
        store.append_turn(session, "old", "answer")
        turn = store.start_turn(session, "next")
        path = next(tmp_path.rglob("*.jsonl"))
        before = path.read_bytes()
        with pytest.raises(ValueError, match="context sources"):
            store.start_request(
                session,
                turn,
                "test",
                context={
                    "strategy": "recent_turns_v1",
                    "selected_turn_ids": ["invented"],
                    "omitted_turn_ids": [],
                    "estimated_input_tokens": 10,
                    "original_history_tokens": 10,
                },
            )
        assert path.read_bytes() == before

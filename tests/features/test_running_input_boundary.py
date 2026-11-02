"""B27.2.1：补充输入只在完整工具批次和请求边界消费，最终回答前原子封口。"""

import json

import pytest

from adam.application.conversation import Conversation
from adam.application.event_queue import EventQueue
from adam.config.settings import AgentConfig
from adam.contracts.events import EventKind
from adam.contracts.models import Completion
from adam.contracts.protocol import validate_turn
from adam.session.storage import SessionStore
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry


@pytest.mark.parametrize("with_tool", [False, True])
def test_input_during_request_reaches_next_request_and_successful_history(tmp_path, with_tool):
    queues = {}
    observed = []
    (tmp_path / "fact.txt").write_text("fact", encoding="utf-8")

    class Model:
        def complete(self, messages, **kwargs):
            observed.append(messages)
            if len(observed) == 1:
                queue = next(iter(queues.values()))
                queue.put(EventKind.USER_INPUT, "terminal", "supplement", "请用中文回答")
                message = {"role": "assistant", "content": "discard this stale final"}
                if with_tool:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "fact.txt"}),
                                },
                            }
                        ],
                    }
                return Completion(message, (1, 1))
            assert messages[-1] == {"role": "user", "content": "请用中文回答"}
            if with_tool:
                assert messages[-2]["role"] == "tool" and messages[-2]["tool_call_id"] == "read"
            return Completion({"role": "assistant", "content": "最终结果"}, (1, 1))

    with SessionStore(tmp_path / "sessions") as store:
        conversation = Conversation(
            store,
            Model(),
            "system",
            ToolExecutor(store, create_tool_registry(tmp_path), lambda _: None),
            AgentConfig(),
            model="test",
            on_turn_started=lambda turn_id: queues.setdefault(turn_id, EventQueue(turn_id)),
            input_provider=queues.get,
        )
        assert conversation.send("original") == "最终结果"
        history = [message.raw for message in store.load_messages(conversation.session.session_id)]
        assert {"role": "user", "content": "请用中文回答"} in history
        assert not any(message.get("content") == "discard this stale final" for message in history)
        assert len(observed) == 2
        with pytest.raises(ValueError, match="closed"):
            next(iter(queues.values())).put(EventKind.USER_INPUT, "terminal", "late", "too late")


def test_protocol_still_rejects_input_inside_tool_pair_or_after_final():
    first = {"role": "user", "content": "task"}
    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "read", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    }
    extra = {"role": "user", "content": "more"}
    final = {"role": "assistant", "content": "done"}
    with pytest.raises(ValueError, match="unmatched"):
        validate_turn([first, call, extra, final])
    with pytest.raises(ValueError, match="after final"):
        validate_turn([first, final, extra])
    validate_turn([first, extra, final])

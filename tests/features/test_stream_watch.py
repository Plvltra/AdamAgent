"""可控时钟验收计时规则，真实流式接入留下一步。"""

import json

import pytest

from adam.config.loader import load_llm_config
from adam.contracts.errors import ConfigError
from adam.llm.stream_watch import StreamDeadlineError, StreamWatch
from adam.llm.streaming import StreamReply
from tests.test_config import LLM


@pytest.mark.parametrize(
    "delta,progress",
    [
        ({"role": "assistant"}, False),
        ({"content": ""}, False),
        ({"content": "answer"}, True),
        ({"reasoning_content": "thinking"}, True),
        (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{"},
                    }
                ]
            },
            True,
        ),
    ],
)
def test_only_nonempty_model_deltas_count_as_progress(delta, progress):
    reply = StreamReply()
    reply.accept(json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
    assert reply.progressed is progress
    reply.accept(json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    assert not reply.progressed


def test_heartbeat_does_not_extend_idle():
    now = [0.0]
    watch = StreamWatch(3, 20, clock=lambda: now[0])
    for value in (1, 2):
        now[0] = value
        watch.received(10)
    assert watch.network_bytes == 20
    now[0] = 3
    with pytest.raises(StreamDeadlineError, match="有效模型输出"):
        watch.received(10)


def test_progress_extends_idle_but_not_total():
    now = [0.0]
    watch = StreamWatch(3, 5, clock=lambda: now[0])
    for value in (2, 4):
        now[0] = value
        watch.progressed()
    now[0] = 5
    with pytest.raises(StreamDeadlineError, match="总时限"):
        watch.progressed()


def test_deadlines_load_and_reject_invalid_values(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text(LLM + "\nsemantic_idle_seconds=2.5\nstream_total_seconds=9\n", encoding="utf-8")
    config = load_llm_config(path)
    assert config.semantic_idle_seconds == 2.5 and config.stream_total_seconds == 9
    path.write_text(LLM + "\nsemantic_idle_seconds=0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_llm_config(path)

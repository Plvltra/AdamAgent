"""计时边界注入到真实LLMClient；SSE解析、关闭和用量处理均走生产代码。"""

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import FailureCode, LLMError
from adam.llm.client import LLMClient
from adam.llm.stream_watch import StreamWatch
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_streaming import DONE, event
from tests.support import build_conversation


@pytest.mark.parametrize("known", [False, True])
def test_deadline_discards_partial_history_and_persists_usage_state(tmp_path, monkeypatch, known):
    now = [0.0]
    monkeypatch.setattr(
        "adam.llm.client.StreamWatch",
        lambda idle, total: StreamWatch(idle, total, clock=lambda: now[0]),
    )
    usage = {"prompt_tokens": 3, "completion_tokens": 2} if known else None
    stream = TimedStream(
        now, [(0, event({"content": "unfinished"}, usage=usage)), (3, b": keepalive\n\n")]
    )
    config = LLMConfig("https://test.invalid", "test", "test-only", semantic_idle_seconds=3)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as http:
        with SessionStore(tmp_path / "sessions") as store:
            current = build_conversation(
                store, LLMClient(config, http), "rules", create_tool_registry(tmp_path)
            )
            with pytest.raises(LLMError):
                current.send("unfinished request")
            session_id = current.session.session_id
            assert store.load_messages(session_id) == []
        with SessionStore(tmp_path / "sessions") as store:
            store.select_session(session_id)
            assert store.load_messages(session_id) == []
            assert store.usage(session_id) == ((5, False) if known else (0, True))
    assert stream.closed


class TimedStream(httpx.SyncByteStream):
    """推进测试时钟后提供实际字节，不需要等待真实秒数。"""

    def __init__(self, now, pieces):
        self.now, self.pieces, self.closed = now, pieces, False

    def __iter__(self):
        for time, data in self.pieces:
            self.now[0] = time
            yield data

    def close(self):
        self.closed = True


@pytest.mark.parametrize("known", [False, True])
def test_heartbeat_deadline_closes_response_and_keeps_only_reported_usage(monkeypatch, known):
    now = [0.0]
    monkeypatch.setattr(
        "adam.llm.client.StreamWatch",
        lambda idle, total: StreamWatch(idle, total, clock=lambda: now[0]),
    )
    usage = {"prompt_tokens": 3, "completion_tokens": 2} if known else None
    stream = TimedStream(
        now,
        [
            (0, event({"content": "partial"}, usage=usage)),
            (1, b": ping\n\n"),
            (3, b": ping\n\n"),
            (4, DONE),
        ],
    )
    requests, output = [], []

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 3
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = LLMClient(
            LLMConfig("https://test.invalid", "test", "test-only", semantic_idle_seconds=3), http
        )
        with pytest.raises(LLMError) as failed:
            client.complete([{"role": "user", "content": "hello"}], on_output=output.append)
    assert failed.value.code == FailureCode.API_TIMEOUT
    assert failed.value.usage == ((3, 2) if known else None)
    assert stream.closed and len(requests) == 1
    assert output[-1].kind == "response_end"


def test_slow_effective_output_completes_within_total_limit(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "adam.llm.client.StreamWatch",
        lambda idle, total: StreamWatch(idle, total, clock=lambda: now[0]),
    )
    stream = TimedStream(
        now,
        [
            (2, event({"content": "a"})),
            (4, event({"content": "b"})),
            (5, event(reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 2})),
            (5, DONE),
        ],
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as http:
        client = LLMClient(
            LLMConfig(
                "https://test.invalid",
                "test",
                "test-only",
                semantic_idle_seconds=3,
                stream_total_seconds=6,
            ),
            http,
        )
        assert client.complete([{"role": "user", "content": "hello"}]).message["content"] == "ab"
    assert stream.closed

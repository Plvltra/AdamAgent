"""V5.1：假 SSE 分片、实际工具与 JSONL；逐步展示不代表消息已经提交。"""

import json

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli import chat
from adam.config import load_llm_config
from adam.errors import ConfigError, LLMError
from adam.llm import LLMClient
from adam.settings import AppConfig, LLMConfig
from adam.storage import SessionStore


def event(delta=None, *, reason=None, usage=None):
    data = {
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}],
        "usage": usage,
    }
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\r\n\r\n").encode()


USAGE = {"prompt_tokens": 10, "completion_tokens": 3}
DONE = b"data: [DONE]\n\n"


class Fragments(httpx.SyncByteStream):
    """故意把 UTF-8、行和事件边界拆散；可在流结束前检查工具是否已执行。"""

    def __init__(self, parts, before_end=None):
        self.parts = parts
        self.before_end = before_end
        self.closed = False

    def __iter__(self):
        for part in self.parts:
            if isinstance(part, BaseException):
                raise part
            for start in range(0, len(part), 7):
                yield part[start : start + 7]
        if self.before_end:
            self.before_end()

    def close(self):
        self.closed = True


def sse(parts):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Fragments(parts)
    )


def test_default_stream_shows_text_once_and_saves_complete_history(tmp_path):
    inputs = iter(["你好", "/exit"])
    output, fragments, bodies = [], [], []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        bodies.append(json.loads(request.content))
        return sse(
            [
                b": keepalive\n\n",
                event({"role": "assistant", "reasoning_content": "内部推理"}),
                event({"content": "你"}),
                event({"content": "好"}),
                event(reason="stop", usage=USAGE),
                DONE,
            ]
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        config = AppConfig(LLMConfig("https://example.test", "test", "secret"), store.root, "")
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        chat(app, read=lambda _: next(inputs), write=output.append, fragment=fragments.append)
        history = app.history()
        assert history[-1].content == "你好"
        assert history[-1].raw["reasoning_content"] == "内部推理"
        assert app.usage().tokens == 13 and not app.usage().unknown
        assert not any(e["type"] == "text_delta" for e in store.load_events(app.session.session_id))
    assert bodies[0]["stream"] is True and bodies[0]["stream_options"] == {"include_usage": True}
    assert fragments[-3:] == ["你", "好", "\n"]
    assert "内部推理" not in "".join(fragments)
    assert not any(line.endswith("你好") for line in output)  # 完整回答不能再打印一次。


def test_tool_arguments_wait_for_complete_stream_and_return_paired_result(tmp_path):
    (tmp_path / "evidence.txt").write_text("evidence", encoding="utf-8")
    bodies, output = [], []
    store = SessionStore(tmp_path / "sessions")

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:

            def fragments():
                yield event(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"'},
                            }
                        ]
                    }
                )
                assert not any(
                    e["type"] == "tool_started" for e in store.load_events(app.session.session_id)
                )
                yield event(
                    {"tool_calls": [{"index": 0, "function": {"arguments": 'evidence.txt"}'}}]}
                )
                yield event(reason="tool_calls", usage=USAGE)
                yield DONE

            return sse(fragments())
        assert bodies[-1]["messages"][-1]["tool_call_id"] == "call1"
        assert "evidence" in bodies[-1]["messages"][-1]["content"]
        return sse([event({"content": "found"}), event(reason="stop", usage=USAGE), DONE])

    with store, httpx.Client(transport=httpx.MockTransport(respond)) as http:
        config = AppConfig(LLMConfig("https://example.test", "test", "secret"), store.root, "")
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        assert app.send("read", on_output=output.append) == "found"
        assert len(app.history()) == 4
        assert app.usage().tokens == 26
    assert [e.text for e in output if e.kind == "text"] == ["found"]


@pytest.mark.parametrize(
    "ending", ["missing_done", "length", "invalid_json", "disconnect", "unknown_usage"]
)
def test_partial_stream_never_enters_history_or_automatically_retries(tmp_path, ending):
    requests, fragments, shown = [], [], []
    parts = [event({"content": "部分回答"})]
    if ending == "missing_done":
        parts.append(event(reason="stop", usage=USAGE))
    elif ending == "length":
        parts.extend([event(reason="length", usage=USAGE), DONE])
    elif ending == "invalid_json":
        parts.append(b"data: not-json\n\n")
    elif ending == "disconnect":
        parts.append(httpx.ReadError("private-server-details"))
    else:
        parts.extend([event(reason="stop"), DONE])

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        requests.append(request)
        return sse(parts)

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        config = AppConfig(LLMConfig("https://example.test", "test", "secret"), store.root, "")
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        inputs = iter(["hello", "/exit"])
        chat(app, read=lambda _: next(inputs), write=shown.append, fragment=fragments.append)
        assert app.history() == []
        events = store.load_events(app.session.session_id)
        assert events[-1]["payload"]["status"] == "failed"
        assert app.usage().unknown == (ending not in {"missing_done", "length"})
        assert "部分回答" not in json.dumps(events, ensure_ascii=False)
    assert len(requests) == 1
    assert "部分回答" in "".join(fragments)
    assert any("未完成" in line for line in shown)
    assert "private-server-details" not in "".join(shown)


@pytest.mark.parametrize(
    "status,content_type", [(400, "application/json"), (200, "application/json")]
)
def test_unsupported_stream_requires_explicit_configuration_change(status, content_type):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LLMError, match="stream=false"):
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http).complete([])
    assert len(requests) == 1


@pytest.mark.parametrize("tail", ["usage_only", "missing_finish", "unfinished_tool"])
def test_protocol_completion_boundaries(tail):
    if tail == "unfinished_tool":
        parts = [event({"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}), DONE]
    else:
        parts = [event({"content": "done"})]
        if tail == "usage_only":
            parts.extend(
                [
                    event(reason="stop"),
                    ("data: " + json.dumps({"choices": [], "usage": USAGE}) + "\n\n").encode(),
                ]
            )
        parts.append(DONE)
    stream = Fragments(parts)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as http:
        client = LLMClient(LLMConfig("https://example.test", "test", "secret"), http)
        if tail == "usage_only":
            assert client.complete([]).usage == (10, 3)
        else:
            with pytest.raises(LLMError):
                client.complete([])
    assert stream.closed


@pytest.mark.parametrize("value", [None, "true", "false", "1", '"false"'])
def test_stream_toml_default_and_validation(tmp_path, value):
    path = tmp_path / "llm.toml"
    path.write_text(
        '[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="secret"\n'
        + (f"stream={value}\n" if value is not None else ""),
        encoding="utf-8",
    )
    if value in {"1", '"false"'}:
        with pytest.raises(ConfigError, match="stream"):
            load_llm_config(path)
    else:
        assert load_llm_config(path).stream is (value != "false")

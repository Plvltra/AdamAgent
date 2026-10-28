"""验证角色展示与按需用量查询，不调用真实模型。"""

import io
import json
import re
from types import SimpleNamespace

import httpx
import pytest

import adam.terminal as terminal
from adam.config import AgentConfig, LLMConfig
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.terminal import role_prefix
from tests.support import run_chat as chat


@pytest.fixture
def interactive_terminal(monkeypatch):
    """模拟可着色终端，让测试不依赖 pytest 捕获模式或本机环境变量。"""
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(stdout=stream))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    return stream


@pytest.mark.parametrize(
    "role,unsaved,label,colored",
    [
        ("user", False, "你> ", True),
        ("assistant", False, "Adam> ", True),
        ("assistant", True, "Adam（未保存）> ", True),
        ("tool", False, "tool> ", False),
    ],
)
def test_role_color_ends_before_body(interactive_terminal, role, unsaved, label, colored):
    prefix = role_prefix(role, unsaved=unsaved)
    assert re.sub(r"\x1b\[[0-9;]*m", "", prefix) == label
    assert prefix.endswith(" ")
    assert ("\x1b[" in prefix) is colored
    if colored:
        displayed = prefix + "正文"
        assert "\x1b[0m" in prefix
        assert "\x1b[" not in displayed.rsplit("\x1b[0m", 1)[1]


@pytest.mark.parametrize("mode", ["redirected", "no_color", "dumb"])
def test_plain_output_when_color_is_disabled(interactive_terminal, monkeypatch, mode):
    if mode == "redirected":
        monkeypatch.setattr(interactive_terminal, "isatty", lambda: False)
    elif mode == "no_color":
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.setenv("TERM", "dumb")
    assert role_prefix("user") == "你> "
    assert role_prefix("assistant") == "Adam> "


def test_empty_no_color_does_not_disable_color(interactive_terminal, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    assert "\x1b[" in role_prefix("user")


def make_http(requests):
    """记录实际请求；模型目录为空，聊天回复报告固定的 15 token 用量。"""

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "reply"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_usage_is_only_shown_on_request_and_not_sent_to_model(tmp_path, interactive_terminal):
    requests = []
    shown = []
    prompts = []
    inputs = iter(["hello", "/usage", "/exit"])

    def read(prompt):
        prompts.append(prompt)
        text = next(inputs)
        if text == "/usage":
            assert not any("用量>" in line for line in shown)
        return text

    with make_http(requests) as http, SessionStore(tmp_path / "sessions") as store:
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=read,
            write=shown.append,
            agent_config=AgentConfig(max_session_tokens=100),
        )
        assert store.usage(store.list_sessions()[0].session_id) == (15, False)
    usage = [line for line in shown if "用量>" in line]
    assert len(usage) == 1 and "15" in usage[0] and "100" in usage[0]
    posts = [request for request in requests if request.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].content)["messages"] == [{"role": "user", "content": "hello"}]
    assert all(prompt == role_prefix("user") for prompt in prompts)
    assert role_prefix("assistant") + "reply" in shown
    for path in (tmp_path / "sessions").rglob("*.jsonl"):
        assert "\x1b" not in path.read_text(encoding="utf-8")
        assert "\\u001b" not in path.read_text(encoding="utf-8")


def test_usage_without_session_does_not_create_or_request(tmp_path):
    requests = []
    shown = []
    inputs = iter(["/usage", "/exit"])
    with make_http(requests) as http, SessionStore(tmp_path / "sessions") as store:
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=shown.append,
        )
        assert store.list_sessions() == []
    assert all(request.method != "POST" for request in requests)
    assert not any("未知命令" in line for line in shown)


def test_usage_unknown_preserves_known_total_without_request(tmp_path):
    requests = []
    shown = []
    inputs = iter(["/usage", "/exit"])
    with make_http(requests) as http, SessionStore(tmp_path / "sessions") as store:
        session_id = store.create_session("usage", "")
        turn_id = store.start_turn(session_id, "usage")
        known = store.start_request(session_id, turn_id, "test")
        store.finish_request(session_id, turn_id, known, (10, 5))
        store.start_request(session_id, turn_id, "test")
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=shown.append,
            resume_id=session_id,
            agent_config=AgentConfig(max_session_tokens=100),
        )
        assert store.usage(session_id) == (15, True)
    usage = "\n".join(line for line in shown if "用量>" in line)
    assert "不完整" in "\n".join(shown) and "15" in usage and "100" in usage
    assert all(request.method != "POST" for request in requests)


def test_resume_uses_same_role_labels(tmp_path, interactive_terminal):
    requests = []
    shown = []
    with make_http(requests) as http, SessionStore(tmp_path / "sessions") as store:
        session_id = store.create_session("history", "")
        store.append_turn(session_id, "old user", "old answer")
        inputs = iter([f"/resume {session_id}", "/exit"])
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=shown.append,
        )
        assert [message.role for message in store.load_messages(session_id)] == [
            "user",
            "assistant",
        ]
    assert role_prefix("user") + "old user" in shown
    assert role_prefix("assistant") + "old answer" in shown
    assert all(request.method != "POST" for request in requests)


def test_usage_follows_selected_session_and_survives_restart(tmp_path):
    """A/B 消费用量不同；切换及重启后 /usage 仍显示当前会话自己的累计值。"""
    root = tmp_path / "sessions"
    requests = []
    shown = []
    inputs = iter(
        [
            "/new A",
            "one",
            "/usage",
            "/new B",
            "two",
            "three",
            "/usage",
            "resume_A",
            "/usage",
            "/exit",
        ]
    )
    with make_http(requests) as http, SessionStore(root) as store:

        def read(_):
            text = next(inputs)
            if text == "resume_A":
                session = next(s for s in store.list_sessions() if s.title == "A")
                return f"/resume {session.session_id}"
            return text

        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=read,
            write=shown.append,
        )
        sessions = {s.title: s.session_id for s in store.list_sessions()}
        assert store.usage(sessions["A"]) == (15, False)
        assert store.usage(sessions["B"]) == (30, False)
    totals = [
        line.split("累计 ", 1)[1].split("/", 1)[0] for line in shown if line.startswith("用量>")
    ]
    assert totals == ["15", "30", "15"]
    shown.clear()
    inputs = iter(["/usage", "/exit"])
    with make_http(requests) as http, SessionStore(root) as store:
        chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=shown.append,
            resume_id=sessions["B"],
        )
    assert any("会话累计 30/" in line for line in shown)
    assert sum(r.method == "POST" for r in requests) == 3

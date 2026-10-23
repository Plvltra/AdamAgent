import json
import sys

import httpx

from adam.cli import chat, main
from adam.config import LLMConfig
from adam.llm import LLMClient
from adam.storage import SessionStore


def make_client(requests):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "answer:" + payload["messages"][-1]["content"],
                        },
                    }
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reopen_resume_isolation_and_failed_switch(tmp_path):
    path = tmp_path / "sessions"
    requests = []
    with make_client(requests) as http, SessionStore(path) as store:
        entries = iter(["/new A", "one", "/new B", "two", "/sessions", "/exit"])
        chat(
            LLMClient(LLMConfig("https://example.test", "model", "secret"), http),
            "old prompt",
            store,
            read=lambda _: next(entries),
            write=lambda _: None,
        )
        sessions = {s.title: s.session_id for s in store.list_sessions()}
        first, second = sessions["A"], sessions["B"]
        assert len(sessions) == 2
    output = []
    with make_client(requests) as http, SessionStore(path) as store:
        entries = iter([f"/resume {first}", "/resume ffffffff", "/resume abc", "three", "/exit"])
        chat(
            LLMClient(LLMConfig("https://example.test", "model", "secret"), http),
            "new prompt",
            store,
            read=lambda _: next(entries),
            write=output.append,
        )
        assert [m.content for m in store.load_messages(first)] == [
            "one",
            "answer:one",
            "three",
            "answer:three",
        ]
        assert [m.content for m in store.load_messages(second)] == ["two", "answer:two"]
    assert requests[-1]["messages"] == [
        {"role": "system", "content": "old prompt"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "answer:one"},
        {"role": "user", "content": "three"},
    ]
    assert any("不存在" in line for line in output)
    assert any("用法" in line for line in output)


def test_save_failure_shows_reply_and_does_not_retry(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    with SessionStore(path) as store:
        session_id = store.create_session("A", "")

    def fail_save(*args):
        raise OSError("injected")

    monkeypatch.setattr(SessionStore, "append_turn", fail_save)
    requests = []
    output = []
    with make_client(requests) as http, SessionStore(path) as store:
        entries = iter(["one", "two", "/exit"])
        chat(
            LLMClient(LLMConfig("https://example.test", "model", "secret"), http),
            "",
            store,
            read=lambda _: next(entries),
            write=output.append,
            resume_id=session_id,
        )
        assert store.load_messages(session_id) == []
    assert len(requests) == 2
    assert len(requests[1]["messages"]) == 1
    assert any("未保存" in line and "answer:one" in line for line in output)


def test_main_creates_session_and_resumes(tmp_path, monkeypatch):
    # 两次调用真实启动入口、两次关闭重开连接，只有 HTTP 和终端输入被替换。
    import adam.cli as cli

    config = tmp_path / "config.toml"
    config.write_text('[chat]\nsystem_prompt="system"\n', encoding="utf-8")
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://example.test"\nmodel="model"\napi_key="test-key"\n',
        encoding="utf-8",
    )
    requests = []
    first_http = make_client(requests)
    second_http = make_client(requests)
    clients = iter([first_http, second_http])
    monkeypatch.setattr(cli.httpx, "Client", lambda **_: next(clients))
    original_chat = cli.chat
    entries = iter(["first", "/exit", "second", "/exit"])

    def interactive(*args, **kwargs):
        return original_chat(*args, **kwargs, read=lambda _: next(entries), write=lambda _: None)

    monkeypatch.setattr(cli, "chat", interactive)
    monkeypatch.setattr(sys, "argv", ["adam", "--config", str(config)])
    assert main() == 0
    with SessionStore(tmp_path / "sessions") as store:
        first = store.list_sessions()[0].session_id
    config.write_text('[chat]\nsystem_prompt="changed"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["adam", "--config", str(config), "--resume", first])
    assert main() == 0
    with SessionStore(tmp_path / "sessions") as store:
        assert len(store.list_sessions()) == 1
        assert [m.content for m in store.load_messages(first)] == [
            "first",
            "answer:first",
            "second",
            "answer:second",
        ]
    assert requests[-1]["messages"][0] == {"role": "system", "content": "system"}
    assert requests[-1]["messages"][1]["content"] == "first"

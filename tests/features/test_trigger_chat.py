"""B29.2.2：模拟HTTP走真实聊天工具、输入留痕与恢复，不调用付费模型。"""

import json

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("live", [True, False])
def test_timer_reaches_chat_only_in_live_mode_and_recovers(tmp_path, live):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body["tools"]}
        assert ("local_trigger" in names) is live
        if live and calls == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "now",
                            "local_trigger",
                            action="timer",
                            delay_seconds=0,
                            text="review result",
                        ),
                        call(
                            "later",
                            "local_trigger",
                            action="timer",
                            delay_seconds=3600,
                            text="old run",
                        ),
                    ]
                ),
            )
        if live:
            assert calls == 2
            assert body["messages"][-2]["role"] == "tool"
            assert "本地条件观察" in body["messages"][-1]["content"]
            assert "不是用户新指令" in body["messages"][-1]["content"]
            assert "review result" in body["messages"][-1]["content"]
            assert len(outputs(body)) == 2
        return httpx.Response(200, json=answer("done"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, live_input=live
        )
        try:
            assert app.send("schedule") == "done"
            sid = app.session.session_id
            if live:
                path = next((store.root / "_inputs" / sid).glob("*.json"))
                events = json.loads(path.read_text(encoding="utf-8"))["events"]
                assert len(events) == 1 and events[0]["kind"] == "timer"
                assert events[0]["source"] == "local_trigger"
                assert events[0]["state"] == "in_context"
        finally:
            app.close()

    def no_request(request):
        pytest.fail("恢复不应发起请求")

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(no_request)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            app.resume(sid)
            if live:
                path = store.root / "_triggers" / sid / "timers.json"
                assert [r["status"] for r in json.loads(path.read_text(encoding="utf-8"))] == [
                    "delivered",
                    "expired",
                ]
                assert "review result" in json.dumps([m.raw for m in app.history()])
        finally:
            app.close()

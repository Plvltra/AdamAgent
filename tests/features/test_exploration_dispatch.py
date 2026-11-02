"""B39.2：真实子线程与模型适配器收尾，宿主验收后停止同组探索。"""

import hashlib
import json
import threading
from contextlib import contextmanager

import httpx
import pytest

from adam.application.subagents.manager import SubagentManager
from adam.bootstrap import build_application
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.verification.citations import CitationVerifier
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("mode", ["pending", "both", "wrong_first"])
def test_verified_winner_cancels_sibling_and_collects_actual_terminal_state(tmp_path, mode):
    (tmp_path / "source.txt").write_bytes(b"needle")
    proof = json.dumps(
        {
            "path": "source.txt",
            "line": 1,
            "content": "needle",
            "sha256": hashlib.sha256(b"needle").hexdigest(),
        }
    )
    loser_entered = threading.Event()
    release_second = threading.Event()
    result = {}
    count = 0
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "Use children",
        agent=AgentConfig(max_session_tokens=200000),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )

    @contextmanager
    def child_client(cancel):
        def child(request):
            body = json.loads(request.content)
            task = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
            if task == "loser":
                loser_entered.set()
                if mode == "both":
                    return httpx.Response(200, json=answer(proof))
                if mode == "wrong_first":
                    assert release_second.wait(5)
                    return httpx.Response(200, json=answer(proof))
                # 模拟等待远端响应；真实令牌检查负责中断，不能把 cancel 当成已完成。
                cancel.wait(10)
                raise AssertionError("winner did not cancel sibling")
            assert loser_entered.wait(5)
            return httpx.Response(
                200, json=answer("fake success" if mode == "wrong_first" else proof)
            )

        with httpx.Client(transport=httpx.MockTransport(child)) as http:
            yield LLMClient(config.llm, http)

    def parent(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("one", "spawn_agent", name="winner", task="winner"),
                        call("two", "spawn_agent", name="loser", task="loser"),
                    ]
                ),
            )
        manager = app._conversation.delegation
        assert isinstance(manager, SubagentManager)
        agents = manager.list_agents()["agents"]
        members = tuple((a["agent_id"], a["run_id"]) for a in agents)
        if mode == "both":
            manager.wait([a["agent_id"] for a in agents], 5)
        elif mode == "wrong_first":
            manager.wait([agents[0]["agent_id"]], 5)
        group_id = manager.create_exploration(
            members, CitationVerifier(tmp_path, ("source.txt",), "needle")
        )
        if mode == "wrong_first":
            first = manager.wait_exploration(group_id, 0)
            assert first["winner"] is None
            assert "running" in first["states"].values()
            release_second.set()
        result.update(manager.wait_exploration(group_id, 5))
        assert set(result["states"].values()) == (
            {"completed", "interrupted"} if mode == "pending" else {"completed"}
        )
        assert result["winner"][0] == agents[1 if mode == "wrong_first" else 0]["agent_id"]
        before = app.usage()
        assert manager.wait_exploration(group_id, 0)["states"] == result["states"]
        assert app.usage() == before
        assert manager.accept_final()
        return httpx.Response(200, json=answer("verified result"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(parent)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, child_client_factory=child_client
        )
        try:
            assert app.send("find evidence") == "verified result"
            saved = (
                store.root
                / "_subagents"
                / app.session.session_id
                / "groups"
                / (result["group_id"] + ".json")
            )
            assert json.loads(saved.read_text())["states"] == result["states"]
            session_id, usage = app.session.session_id, app.usage()
        finally:
            app.close()

    def no_requests(request):
        pytest.fail("recovery must not call any model")

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(no_requests)) as http,
    ):
        reopened = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, child_client_factory=child_client
        )
        try:
            reopened.resume(session_id)
            assert reopened.usage() == usage
            manager = reopened._conversation.delegation
            assert isinstance(manager, SubagentManager)
            restored = manager.wait_exploration(result["group_id"], 0)
            assert restored["winner"] == result["winner"]
            assert restored["states"] == result["states"]
            assert reopened.usage() == usage
        finally:
            reopened.close()

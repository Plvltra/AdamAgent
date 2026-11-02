"""B39.2：真实子线程与模型适配器收尾，宿主验收后停止同组探索。"""

import hashlib
import json
import threading
from contextlib import contextmanager

import httpx

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


def test_verified_winner_cancels_sibling_and_collects_actual_terminal_state(tmp_path):
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
                # 模拟等待远端响应；真实令牌检查负责中断，不能把 cancel 当成已完成。
                cancel.wait(10)
                raise AssertionError("winner did not cancel sibling")
            assert loser_entered.wait(5)
            return httpx.Response(200, json=answer(proof))

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
        group_id = manager.create_exploration(
            members, CitationVerifier(tmp_path, ("source.txt",), "needle")
        )
        result.update(manager.wait_exploration(group_id, 5))
        assert set(result["states"].values()) == {"completed", "interrupted"}
        assert result["winner"][0] == agents[0]["agent_id"]
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
        finally:
            app.close()

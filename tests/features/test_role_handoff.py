"""B41.2：真实应用移交有限背景、角色工具边界和失败恢复。"""

import json
from contextlib import contextmanager

import httpx
import pytest

from adam.application.handoff import RoleHandoff
from adam.bootstrap import build_application
from adam.config.roles import RoleProfile
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.contracts.errors import AgentError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@contextmanager
def role_app(tmp_path, workspace, name, handler, approve=None):
    role = RoleProfile(
        name,
        "test",
        frozenset({"read_file"} if name == "investigator" else {"read_file", "replace_text"}),
        4,
    )
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / name,
        "host rules",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), workspace, role=role, confirm_edit=approve
        )
        try:
            yield app
        finally:
            app.close()


@pytest.mark.parametrize("failure", [False, True])
def test_explicit_handoff_uses_bounded_context_and_preserves_failure(tmp_path, failure):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "a.txt").write_text("old")
    counts = {"source": 0, "target": 0}
    approvals = []

    def source_handler(request):
        counts["source"] += 1
        body = json.loads(request.content)
        assert {tool["function"]["name"] for tool in body["tools"]} == {"read_file"}
        if counts["source"] == 1:
            result = tool_response(
                [
                    call(
                        "forbidden",
                        "replace_text",
                        path="a.txt",
                        old_text="old",
                        new_text="bad",
                        expected_version="fake",
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["ok"] is False
            result = answer("a.txt needs old changed to new")
        return httpx.Response(200, json=result)

    def target_handler(request):
        counts["target"] += 1
        body = json.loads(request.content)
        assert "source-private-unrelated-chat" not in json.dumps(body)
        assert "a.txt needs old changed to new" in json.dumps(body)
        if failure:
            return httpx.Response(400, json={"error": {"message": "injected"}})
        if counts["target"] == 1:
            result = tool_response([call("r", "read_file", path="a.txt")])
        elif counts["target"] == 2:
            result = tool_response(
                [
                    call(
                        "w",
                        "replace_text",
                        path="a.txt",
                        old_text="old",
                        new_text="new",
                        expected_version=outputs(body)[-1]["version"],
                    )
                ]
            )
        else:
            result = answer("done")
        return httpx.Response(200, json=result)

    with (
        role_app(tmp_path, workspace, "investigator", source_handler) as source,
        role_app(
            tmp_path,
            workspace,
            "executor",
            target_handler,
            lambda plan: approvals.append(plan.path) is None,
        ) as target,
    ):
        source.send("source-private-unrelated-chat: investigate a.txt")
        assert (workspace / "a.txt").read_text() == "old"
        with pytest.raises(AgentError, match="角色模型"):
            source.select_model("unconfigured-model")
        handoffs = RoleHandoff(tmp_path / "handoffs")
        target.select_mode("plan")
        with pytest.raises(AgentError, match="模式"):
            handoffs.create(source, target, "fix a.txt", "preserve other files")
        target.select_mode("act")
        key = handoffs.create(source, target, "fix a.txt", "preserve other files")
        if failure:
            with pytest.raises(AgentError):
                handoffs.execute(key, target)
            assert handoffs.read(key)["status"] == "failed"
            assert not approvals
        else:
            assert handoffs.execute(key, target) == "done"
            assert handoffs.read(key)["status"] == "succeeded"
            assert len(approvals) == 1
            assert (workspace / "a.txt").read_text() == "new"
        before = dict(counts)
        with pytest.raises(AgentError):
            handoffs.execute(key, target)
        assert RoleHandoff(handoffs.root).recover() == []
        assert counts == before


def test_recovery_marks_inflight_handoff_without_invoking_any_agent(tmp_path):
    handoffs = RoleHandoff(tmp_path)
    key = "a" * 16
    # 注入强杀时已经落盘、但目标尚未收尾的宿主记录。
    (tmp_path / f"{key}.json").write_text(json.dumps({"status": "running"}))
    assert handoffs.recover() == [key]
    assert handoffs.read(key)["status"] == "interrupted"
    assert handoffs.recover() == []

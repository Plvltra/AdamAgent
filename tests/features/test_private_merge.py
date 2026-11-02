"""B40.2：子模型在副本真实编辑，主项目审批后合并并跨文件验收。"""

import json
import subprocess
from contextlib import contextmanager

import httpx

from adam.application.subagents.manager import SubagentManager
from adam.application.subagents.private_merge import PrivateMerge
from adam.bootstrap import build_application
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    PermissionConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy
from adam.verification.files import FileStateVerifier
from tests.features.test_private_workspaces import make_repo
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_private_child_edits_two_files_then_host_approves_and_verifies(tmp_path):
    source = tmp_path / "source"
    make_repo(source)
    (source / "second.txt").write_text("second", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "core.hooksPath=",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "root rules",
        agent=AgentConfig(max_session_tokens=200000),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    child_count = parent_count = 0
    approvals, copies = [], []

    def child(request):
        nonlocal child_count
        child_count += 1
        body = json.loads(request.content)
        assert "preserve contract" in json.dumps(body)
        if child_count == 1:
            result = tool_response(
                [
                    call("r1", "read_file", path="source.txt"),
                    call("r2", "read_file", path="second.txt"),
                ]
            )
        elif child_count == 2:
            read = outputs(body)
            assert all(item["ok"] for item in read)
            assert all(not item["path"].startswith(str(source)) for item in read)
            result = tool_response(
                [
                    call(
                        "e1",
                        "replace_text",
                        path="source.txt",
                        old_text="original",
                        new_text="fixed",
                        expected_version=read[0]["version"],
                    ),
                    call(
                        "e2",
                        "replace_text",
                        path="second.txt",
                        old_text="second",
                        new_text="aligned",
                        expected_version=read[1]["version"],
                    ),
                ]
            )
        else:
            assert all(item["status"] == "applied" for item in outputs(body)[-2:])
            result = answer("private changes ready")
        return httpx.Response(200, json=result)

    @contextmanager
    def child_client(cancel):
        with httpx.Client(transport=httpx.MockTransport(child)) as http:
            yield LLMClient(config.llm, http)

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [call("s", "spawn_agent", name="editor", task="fix both files")]
                ),
            )
        manager = app._conversation.delegation
        assert isinstance(manager, SubagentManager)
        item = manager.wait([], 5)["agents"][0]
        assert item["status"] == "completed", item
        service, copy = manager.private_workspace(item["agent_id"], item["run_id"])
        copies.append(copy)
        assert copy.rules_root == copy.memory_identity == source
        assert (source / "source.txt").read_text() == "original"

        def approve(plan):
            approvals.append(plan.path)
            return True

        merge = PrivateMerge(
            service,
            EditTools(PathPolicy(source, PermissionConfig()), config.tool_limits, approve),
            FileStateVerifier(source, {"source.txt": b"fixed", "second.txt": b"aligned"}),
        )
        proposal = merge.collect(copy)
        outcome = merge.apply(proposal)
        assert outcome["status"] == "verified"
        assert len(outcome["results"]) == 2
        assert proposal.exists()
        return httpx.Response(200, json=answer("merged verified changes"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(parent)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            source,
            child_client_factory=child_client,
            private_children=True,
        )
        try:
            assert app.send("fix both files") == "merged verified changes"
            assert len(approvals) == 2
        finally:
            app.close()
    assert not copies[0].path.exists()
    assert (copies[0].path.parent / "proposal.json").exists()
    assert (source / "source.txt").read_text() == "fixed"

"""B40.3：真实双子任务竞争、越界与取消；主项目副作用须经宿主合并。"""

import json
import subprocess
import threading
from contextlib import contextmanager

import httpx
import pytest

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
from adam.contracts.errors import ToolError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy
from adam.verification.files import FileStateVerifier
from tests.features.test_private_workspaces import make_repo
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("mode", ["disjoint", "conflict", "cancel"])
def test_two_private_tasks_merge_or_preserve_conflict_and_cancel(tmp_path, mode):
    source = tmp_path / "source"
    make_repo(source)
    (source / "second.txt").write_text("original", encoding="utf-8")
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
        "rules",
        agent=AgentConfig(max_session_tokens=200000),
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    blocked = threading.Event()
    parent_count = 0
    copy_paths = []
    approvals = []

    @contextmanager
    def child_client(cancel):
        step = 0

        def child(request):
            nonlocal step
            step += 1
            body = json.loads(request.content)
            task = next(
                m["content"]
                for m in body["messages"]
                if m["role"] == "user" and m["content"].startswith("worker")
            )
            name = "second.txt" if task == "worker2" and mode == "disjoint" else "source.txt"
            if step == 1:
                result = tool_response([call("r", "read_file", path=name)])
            elif step == 2:
                version = outputs(body)[0]["version"]
                result = tool_response(
                    [
                        call(
                            "outside",
                            "replace_text",
                            path=str(source / name),
                            old_text="original",
                            new_text="outside",
                            expected_version=version,
                        ),
                        call(
                            "edit",
                            "replace_text",
                            path=name,
                            old_text="original",
                            new_text=task,
                            expected_version=version,
                        ),
                    ]
                )
            else:
                facts = outputs(body)[-2:]
                assert facts[0]["ok"] is False
                assert facts[1]["status"] == "applied"
                if mode == "cancel":
                    blocked.set()
                    cancel.wait(20)
                    raise AssertionError("child was not cancelled")
                result = answer("copy ready")
            return httpx.Response(200, json=result)

        with httpx.Client(transport=httpx.MockTransport(child)) as http:
            yield LLMClient(config.llm, http)

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            tasks = [call("s1", "spawn_agent", name="first", task="worker1")]
            if mode != "cancel":
                tasks.append(call("s2", "spawn_agent", name="second", task="worker2"))
            return httpx.Response(200, json=tool_response(tasks))
        manager = app._conversation.delegation
        if mode == "cancel":
            assert blocked.wait(10)
            member = manager.list_agents()["agents"][0]
            manager.interrupt(member["agent_id"])
            assert manager.wait([], 5)["agents"][0]["status"] == "interrupted"
            assert (source / "source.txt").read_text() == "original"
            return httpx.Response(200, json=answer("cancelled without merge"))
        members = manager.wait([], 10)["agents"]
        assert all(member["status"] == "completed" for member in members), members
        collected = []
        for index, member in enumerate(members):
            service, copy = manager.private_workspace(member["agent_id"], member["run_id"])
            copy_paths.append(copy.path)
            expected = {"source.txt": b"worker1"}
            if index == 1 and mode == "disjoint":
                expected["second.txt"] = b"worker2"
            merge = PrivateMerge(
                service,
                EditTools(
                    PathPolicy(source, PermissionConfig()),
                    config.tool_limits,
                    lambda plan: approvals.append(plan.path) is None,
                ),
                FileStateVerifier(source, expected),
            )
            collected.append((merge, merge.collect(copy)))
        assert (source / "source.txt").read_text() == "original"
        assert collected[0][0].apply(collected[0][1])["status"] == "verified"
        if mode == "conflict":
            with pytest.raises(ToolError, match="已变化"):
                collected[1][0].apply(collected[1][1])
            assert len(approvals) == 1
        else:
            assert collected[1][0].apply(collected[1][1])["status"] == "verified"
            assert len(approvals) == 2
        return httpx.Response(200, json=answer("done"))

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
            app.send("work in private copies")
        finally:
            app.close()
    assert all(not path.exists() for path in copy_paths)
    assert not list((config.sessions_dir / "_private").glob("*/*/repo"))

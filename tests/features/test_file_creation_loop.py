"""B22.3.1：真实生产创建、发现、批准及撤销；仅远端 HTTP 使用替身。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ApprovalDeniedError
from adam.recording.runtime import replay_turn
from tests.features.test_edit_diagnostics import structured_hooks
from tests.features.test_hook_acceptance import example_hooks
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application as child_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import application, names, settings
from tests.features.test_tool_hooks import hooked_app, model_for
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def creation_then_edit(body, number):
    """从按需发现开始，创建后实际读取版本，再沿用原替换工具修改。"""
    if number == 1:
        assert names(body) == ["list_tools", "discover_tools"]
        return tool_response(
            [call("discover", "discover_tools", names=["create_file", "read_file", "replace_text"])]
        )
    if number == 2:
        assert "create_file" in names(body)
        return tool_response([call("create", "create_file", path="new.txt", content="first\n")])
    if number == 3:
        assert outputs(body)[-1]["status"] == "applied"
        return tool_response([call("read", "read_file", path="new.txt")])
    if number == 4:
        return tool_response(
            [
                call(
                    "edit",
                    "replace_text",
                    path="new.txt",
                    old_text="first",
                    new_text="second",
                    expected_version=outputs(body)[-1]["version"],
                )
            ]
        )
    assert outputs(body)[-1]["status"] == "applied"
    return answer("created and edited")


def test_discover_create_read_edit_undo_and_replay_without_recreating(tmp_path, monkeypatch):
    config = replace(config_at(tmp_path), tool_discovery=settings(tmp_path).tool_discovery)

    def respond(body, number):
        return httpx.Response(200, json=creation_then_edit(body, number))

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "created and edited"
        target = tmp_path / "new.txt"
        assert target.read_bytes() == b"second\n"
        edits = store.load_edits(app.session.session_id)
        # 原替换日志不补写新字段；缺省语义仍是replace，创建必须显式保留create。
        assert (
            edits[0]["operation"] == "create" and edits[1].get("operation", "replace") == "replace"
        )
        assert [edit["status"] for edit in edits] == ["applied", "applied"]
        assert len(bodies) == 5
        app.undo()
        assert target.read_bytes() == b"first\n"
        app.undo()
        assert not target.exists()
        frozen = store.load_events(app.session.session_id)
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert not target.exists()
    assert store.load_events(app.session.session_id) == frozen


def test_creation_rejects_same_batch_discovery_before_approval(tmp_path):
    def respond(body, number):
        if number == 1:
            return tool_response(
                [
                    call("discover", "discover_tools", names=["create_file"]),
                    call("early", "create_file", path="new.txt", content="x"),
                ]
            )
        assert "本次请求尚未提供" in outputs(body)[-1]["error"]
        return answer("not created")

    with application(tmp_path, respond) as (app, store, _):
        assert app.send("create") == "not created"
        assert store.load_edits(app.session.session_id) == []
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("boundary", ["plan", "denied", "read_only", "invalid_arguments"])
def test_production_creation_boundaries_never_write(tmp_path, boundary):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = outside / "new.txt" if boundary == "read_only" else workspace / "new.txt"
    config = replace(config_at(workspace), permissions=PermissionConfig((outside,), ()))
    approvals = []

    def approve(plan):
        approvals.append(plan)
        return False

    def respond(body, number):
        if number == 1:
            args = {"path": str(target), "content": "x"}
            if boundary == "invalid_arguments":
                args["unrecognized"] = "x"
            return tool_response([call("create", "create_file", **args)])
        assert not outputs(body)[-1]["ok"]
        if boundary == "plan":
            assert outputs(body)[-1]["status"] == "mode_blocked"
        return answer("not created")

    with application(workspace, respond, config=config, approve=approve) as (app, store, bodies):
        if boundary == "plan":
            app.select_mode("plan")
        if boundary == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("create")
            assert len(bodies) == 1 and len(approvals) == 1
            assert store.load_edits(app.session.session_id)[0]["status"] == "denied"
        else:
            assert app.send("create") == "not created"
            assert approvals == [] and store.load_edits(app.session.session_id) == []
    assert not target.exists()


@pytest.mark.parametrize("structured", [False, True], ids=["original-hook", "diagnostic-hook"])
def test_creation_runs_real_syntax_hook_and_keeps_edit_success_separate(tmp_path, structured):
    marker = tmp_path / "must-not-run"
    content = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nvalue =\n"
    bodies = []
    hooks = structured_hooks() if structured else example_hooks()
    with hooked_app(
        tmp_path,
        model_for([call("create", "create_file", path="new.py", content=content)], bodies),
        hooks,
        approve=lambda _: True,
    ) as (app, store):
        assert app.send("create") == "done"
        edit = store.load_edits(app.session.session_id)[0]
        assert edit["status"] == "applied" and not marker.exists()
        hook = store.load_hooks(app.session.session_id)[-1]
        if structured:
            assert hook["diagnostics"]["status"] == "failed"
            assert hook["diagnostics"]["items"][0]["line"] == 3
            assert hook["diagnostics"]["expected_version"] == edit["after_version"]
            assert any("SyntaxError" in (m.get("content") or "") for m in bodies[-1]["messages"])
        else:
            assert hook["status"] == "failed" and hook["exit_code"] == 1
        assert outputs(bodies[-1])[-1]["status"] == "applied"
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == content


def test_creation_respects_directory_hook_before_approval(tmp_path):
    (tmp_path / "vendor").mkdir()
    bodies, approvals = [], []
    with hooked_app(
        tmp_path,
        model_for([call("create", "create_file", path="vendor/new.py", content="x=1")], bodies),
        example_hooks(),
        approve=approvals.append,
    ) as (app, store):
        assert app.send("create") == "done"
        assert outputs(bodies[-1])[-1]["status"] == "hook_blocked"
        assert store.load_edits(app.session.session_id) == [] and approvals == []
    assert not (tmp_path / "vendor/new.py").exists()


def test_delegated_creation_returns_to_parent_without_requesting_approval(tmp_path):
    parent_bodies, child_bodies = [], []

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        if len(parent_bodies) == 1:
            value = tool_response([call("spawn", "spawn_agent", name="helper", task="create file")])
        elif len(parent_bodies) == 2:
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            value = answer("parent must approve")
        return httpx.Response(200, json=value)

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        if len(child_bodies) == 1:
            value = tool_response([call("create", "create_file", path="new.txt", content="x")])
        else:
            assert outputs(body)[-1]["status"] == "approval_required"
            value = answer("parent must approve")
        return httpx.Response(200, json=value)

    with child_application(tmp_path, parent, child) as (app, _):
        assert app.send("delegate") == "parent must approve"
    assert len(child_bodies) == 2 and not (tmp_path / "new.txt").exists()

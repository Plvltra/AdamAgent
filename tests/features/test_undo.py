"""T7.2：假模型走标准组装，真实编辑、检查点、用户 /undo 和 JSONL。"""

import json
import sys
from pathlib import Path

import httpx
import pytest

import adam.tool_calls.edit_tools as edit_module
from adam.application.undo import UndoService
from adam.cli.main import CommandResult, confirm_edit, handle_command
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import (
    AgentError,
    ApprovalDeniedError,
    EditLogError,
    StorageError,
    ToolError,
)
from adam.contracts.execution import ExecutionMode
from adam.session.checkpoints import CheckpointStore
from adam.session.diagnostics import read_report
from adam.tool_calls.edit_plan import EditConflictError
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_loop import answer, tool_response


@pytest.fixture
def edit_app(tmp_path):
    target = tmp_path / "source.txt"
    original = b"\xef\xbb\xbfold\r\n" + "尾行".encode()
    target.write_bytes(original)
    requests, approvals, shown = [], [], []
    control = {"approve": True, "old": "old", "new": "new", "terminal": False}

    def confirm(plan):
        approvals.append(plan)
        if callback := control.get("confirm_hook"):
            callback(plan)
        if control["terminal"]:
            return confirm_edit(plan, lambda _: "y" if control["approve"] else "n", shown.append)
        return control["approve"]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        phase = (len(requests) - 1) % 3
        if phase == 0:
            result = tool_response([call("read", "read_file", path="source.txt")])
        elif phase == 1:
            version = outputs(body)[-1]["version"]
            result = tool_response(
                [
                    call(
                        "edit",
                        "replace_text",
                        path="source.txt",
                        old_text=control["old"],
                        new_text=control["new"],
                        expected_version=version,
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["status"] == "applied"
            result = answer("已修改")
        return httpx.Response(200, json=result)

    with plan_app(tmp_path, respond, confirm=confirm) as (app, store):
        yield app, store, target, original, requests, approvals, shown, control


def test_model_edit_saves_snapshot_and_cli_undo_preserves_bytes_history_and_usage(edit_app):
    app, store, target, original, requests, approvals, shown, control = edit_app
    control["terminal"] = True
    app.send("将 old 改为 new")
    sid = app.session.session_id
    history, usage = app.history(), app.usage()
    edit = store.load_edits(sid)[0]
    saved = store.load_edit_checkpoint(sid, edit["edit_id"], max_bytes=1024)
    assert saved.original == original
    assert handle_command("/undo", app, shown.append) is CommandResult.HANDLED
    assert target.read_bytes() == original
    assert "-new" in approvals[-1].diff and "+old" in approvals[-1].diff
    assert len(approvals) == 2 and any("已撤销文件修改" in line for line in shown)
    assert approvals[-1].diff in shown
    assert len(requests) == 3 and app.history() == history and app.usage() == usage
    undo = store.load_undos(sid)[0]
    assert undo["status"] == "applied" and undo["after_version"] == edit["before_version"]
    assert undo["edit_id"] == edit["edit_id"] and undo["source_turn_id"] == edit["turn_id"]
    assert undo["source_request_id"] == edit["request_id"]
    assert undo["source_tool_call_id"] == edit["tool_call_id"]
    assert undo["duration_ms"] >= 0 and store.load_edits(sid)[0] == edit
    assert not store.has_unconfirmed_actions(sid)
    report = read_report(next(store.root.glob("*/*/*/*.jsonl")))
    assert report["undos"] == [undo]
    assert not any(e["type"] == "edit_checkpoint" for e in store.load_events(sid))


def test_undo_selects_last_successful_edit_only(edit_app):
    app, store, target, original, requests, approvals, _, control = edit_app
    app.send("first edit")
    control.update(old="new", new="third")
    app.send("second edit")
    sid = app.session.session_id
    edits = store.load_edits(sid)
    result = app.undo()
    assert result.edit_id == edits[-1]["edit_id"] != edits[0]["edit_id"]
    assert target.read_bytes() == original.replace(b"old", b"new")
    assert len(requests) == 6 and len(approvals) == 3


def test_denied_undo_keeps_edit_and_remains_available(edit_app):
    app, store, target, original, requests, approvals, shown, control = edit_app
    app.send("edit")
    sid = app.session.session_id
    control["approve"] = False
    handle_command("/undo", app, shown.append)
    assert target.read_bytes() == original.replace(b"old", b"new")
    assert store.load_undos(sid)[0]["status"] == "denied"
    assert any("已取消撤销" in line for line in shown)
    assert store.undo_checkpoint(sid, max_bytes=1024) is not None
    assert len(requests) == 3 and len(approvals) == 2


@pytest.mark.parametrize("when", ["before", "during_confirmation"])
def test_external_changes_are_preserved_and_conflict_is_logged(edit_app, when):
    app, store, target, _, requests, approvals, _, control = edit_app
    app.send("edit")
    if when == "before":
        target.write_bytes(b"external")
    else:
        control["confirm_hook"] = lambda plan: target.write_bytes(b"external")
    with pytest.raises(EditConflictError):
        app.undo()
    undo = store.load_undos(app.session.session_id)[0]
    assert undo["status"] == "conflict" and undo["after_version"] is None
    assert target.read_bytes() == b"external" and len(requests) == 3
    assert len(approvals) == (1 if when == "before" else 2)


def test_snapshot_does_not_grant_new_write_permission(edit_app, tmp_path):
    app, store, target, original, requests, approvals, _, _ = edit_app
    app.send("edit")
    restricted_root = tmp_path / "restricted"
    restricted_root.mkdir()
    editor = EditTools(
        PathPolicy(restricted_root, PermissionConfig()),
        ToolLimits(),
        lambda plan: approvals.append(plan) or True,
    )
    service = UndoService(store, editor, lambda: ExecutionMode.ACT)
    with pytest.raises(ToolError, match="未授权"):
        service.undo(app.session.session_id)
    assert len(approvals) == 1 and len(requests) == 3
    assert target.read_bytes() == original.replace(b"old", b"new")
    assert store.load_undos(app.session.session_id)[0]["status"] == "failed"


def test_undo_preflight_io_failure_is_controlled_and_recorded(edit_app, monkeypatch):
    app, store, target, original, requests, approvals, _, _ = edit_app
    app.send("edit")

    def fail(self, value):
        raise OSError("injected private OS detail")

    monkeypatch.setattr(PathPolicy, "resolve_write", fail)
    with pytest.raises(ToolError, match="撤销目标无法读取") as caught:
        app.undo()
    assert "private OS detail" not in str(caught.value)
    assert target.read_bytes() == original.replace(b"old", b"new") and len(approvals) == 1
    result = store.load_undos(app.session.session_id)[0]
    assert result["status"] == "failed" and "private OS detail" not in result["error"]
    assert len(requests) == 3


def test_plan_mode_blocks_undo_before_approval_or_log(edit_app):
    app, store, target, original, requests, approvals, _, control = edit_app
    app.send("edit")
    app.select_mode("plan")
    with pytest.raises(ToolError, match="plan 模式"):
        app.undo()
    assert store.load_undos(app.session.session_id) == [] and len(approvals) == 1
    assert target.read_bytes() == original.replace(b"old", b"new")
    app.select_mode("act")

    def cannot_switch_or_reenter(plan):
        with pytest.raises(AgentError, match="尚未结束"):
            app.select_mode("plan")
        with pytest.raises(AgentError, match="运行中的任务"):
            app.undo()

    control["confirm_hook"] = cannot_switch_or_reenter
    assert app.undo().status == "applied" and len(requests) == 3
    app.select_mode("plan")


def test_commands_without_session_or_with_arguments_do_not_create_data(edit_app):
    app, store, _, _, requests, approvals, shown, _ = edit_app
    handle_command("/undo extra", app, shown.append)
    with pytest.raises(AgentError, match="尚无会话"):
        handle_command("/undo", app, shown.append)
    assert store.list_sessions() == [] and requests == approvals == []
    assert shown == ["用法：/undo"]


def test_checkpoint_failure_stops_model_edit_before_side_effect(edit_app, monkeypatch):
    app, store, target, original, requests, _, _, _ = edit_app

    def fail(self, checkpoint):
        raise StorageError("injected checkpoint save failure")

    monkeypatch.setattr(CheckpointStore, "put", fail)
    with pytest.raises(EditLogError):
        app.send("edit")
    sid = app.session.session_id
    assert len(requests) == 2 and target.read_bytes() == original
    assert store.load_edits(sid)[0]["status"] == "planned"
    assert not any(e["type"] == "edit_started" for e in store.load_events(sid))
    assert store.load_messages(sid) == []


def test_denied_edit_never_creates_snapshot(edit_app):
    app, store, target, original, requests, _, _, control = edit_app
    control["approve"] = False
    with pytest.raises(ApprovalDeniedError):
        app.send("edit")
    assert len(requests) == 2 and target.read_bytes() == original
    assert not list((store.root / "_checkpoints").rglob("*.json"))


@pytest.mark.parametrize(
    "boundary",
    [
        "undo_requested",
        "undo_prepared",
        "undo_started",
        "undo_finished",
    ],
)
def test_undo_log_failure_never_claims_success_or_repeats_action(edit_app, monkeypatch, boundary):
    app, store, target, original, requests, _, _, _ = edit_app
    app.send("edit")
    sid = app.session.session_id
    append = store.append_undo_event

    def fail(session_id, kind, payload):
        if kind == boundary:
            raise StorageError("injected undo log boundary")
        append(session_id, kind, payload)

    monkeypatch.setattr(store, "append_undo_event", fail)
    with pytest.raises(EditLogError):
        app.undo()
    expected = original if boundary == "undo_finished" else original.replace(b"old", b"new")
    assert target.read_bytes() == expected and len(requests) == 3
    if boundary == "undo_requested":
        assert store.load_undos(sid) == []
    else:
        assert app.continuation_status().allowed is False
        assert (
            store.load_undos(sid)[0]["status"]
            == {
                "undo_prepared": "requested",
                "undo_started": "prepared",
                "undo_finished": "unknown",
            }[boundary]
        )
        with pytest.raises(StorageError) as caught:
            app.send("must not request")
        assert caught.value.code == "execution_unknown"
        assert len(requests) == 3


def test_undo_replace_failure_preserves_file_and_removes_own_temporary(edit_app, monkeypatch):
    app, store, target, original, requests, _, _, _ = edit_app
    app.send("edit")
    replace = edit_module.os.replace

    def fail_target(source, destination):
        if Path(destination) == target:
            raise OSError("injected undo replace failure")
        return replace(source, destination)

    monkeypatch.setattr(edit_module.os, "replace", fail_target)
    with pytest.raises(ToolError, match="写入失败"):
        app.undo()
    assert target.read_bytes() == original.replace(b"old", b"new")
    assert not list(target.parent.glob(".adam-edit-*"))
    assert store.load_undos(app.session.session_id)[0]["status"] == "failed"
    assert app.continuation_status().allowed and len(requests) == 3


@pytest.mark.parametrize("when", ["confirmation", "after_apply"])
def test_cancel_keeps_known_and_unknown_undo_outcomes_separate(edit_app, monkeypatch, when):
    app, store, target, original, requests, _, _, control = edit_app
    app.send("edit")
    apply = EditTools.apply

    def cancel(plan):
        raise KeyboardInterrupt

    def apply_then_cancel(self, plan):
        apply(self, plan)
        raise KeyboardInterrupt

    if when == "confirmation":
        control["confirm_hook"] = cancel
    else:
        monkeypatch.setattr(EditTools, "apply", apply_then_cancel)
    with pytest.raises(KeyboardInterrupt):
        app.undo()
    undo = store.load_undos(app.session.session_id)[0]
    assert undo["status"] == ("interrupted" if when == "confirmation" else "unknown")
    assert target.read_bytes() == (
        original.replace(b"old", b"new") if when == "confirmation" else original
    )
    assert app.continuation_status().allowed is (when == "confirmation")
    assert len(requests) == 3


@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_latest_missing_snapshot_does_not_fall_back_to_older_edit(edit_app, damage):
    app, store, target, original, requests, approvals, _, control = edit_app
    app.send("first")
    control.update(old="new", new="third")
    app.send("second")
    sid = app.session.session_id
    latest = store.load_edits(sid)[-1]
    path = store.root / "_checkpoints" / sid / f"{latest['edit_id']}.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b'{"version":1,')
    with pytest.raises(StorageError):
        app.undo()
    assert target.read_bytes() == original.replace(b"old", b"third")
    assert store.load_undos(sid) == [] and len(approvals) == 2 and len(requests) == 6


def test_undo_without_confirmation_callback_is_denied(edit_app):
    app, store, target, original, requests, _, _, _ = edit_app
    app.send("edit")
    service = UndoService(
        store,
        EditTools(PathPolicy(target.parent, PermissionConfig()), ToolLimits()),
        lambda: ExecutionMode.ACT,
    )
    result = service.undo(app.session.session_id)
    assert result.status == "denied" and target.read_bytes() == original.replace(b"old", b"new")
    assert store.load_undos(app.session.session_id)[0]["status"] == "denied"
    assert len(requests) == 3


def test_shell_change_is_not_an_undo_candidate(tmp_path):
    target = tmp_path / "from-shell.txt"
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        result = (
            tool_response(
                [
                    call(
                        "command",
                        "run_command",
                        argv=[
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('from-shell.txt').write_text('shell')",
                        ],
                    )
                ]
            )
            if len(requests) == 1
            else answer("command done")
        )
        return httpx.Response(200, json=result)

    with plan_app(tmp_path, respond, confirm=lambda _: True) as (app, store):
        app.send("run command")
        with pytest.raises(AgentError, match="没有尚未撤销"):
            app.undo()
        assert store.load_edits(app.session.session_id) == []
        assert store.load_undos(app.session.session_id) == []
        assert target.read_text() == "shell" and len(requests) == 2

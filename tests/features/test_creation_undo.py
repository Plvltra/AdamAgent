"""B22.2.3.2：只从成功创建撤销至缺失，真实批准/文件/日志和保存故障。"""

from pathlib import Path

import pytest

from adam.application.undo import UndoService
from adam.cli.main import confirm_edit
from adam.contracts.errors import AgentError, EditLogError, StorageError, ToolError
from adam.contracts.execution import ExecutionMode
from adam.contracts.models import DeletePlan
from adam.session.storage import SessionStore
from adam.tool_calls.edit_plan import EditConflictError
from tests.features.test_creation_records import creation_chat as creation_chat


def service(fixture):
    conversation, editor, _, _, _, _ = fixture
    return UndoService(conversation.store, editor, lambda: ExecutionMode.ACT)


@pytest.mark.parametrize("content", ["", "created\n"], ids=["empty", "text"])
def test_approved_creation_undo_removes_file_and_persists_absence(creation_chat, content):
    conversation, editor, control, requests, approvals, target = creation_chat
    control["content"] = content
    conversation.send("create")
    sid, store = conversation.session.session_id, conversation.store
    shown = []
    editor.confirm = lambda plan: confirm_edit(plan, lambda _: "y", shown.append)
    result = service(creation_chat).undo(sid)
    assert result.status == "applied" and not target.exists()
    assert shown[0] == f"删除文件> {target}" and "+++ /dev/null" in shown[1]
    assert len(requests) == 2 and len(approvals) == 1
    undo = store.load_undos(sid)[0]
    assert undo["status"] == "applied" and undo["target_version"] is None
    assert undo["after_version"] is None and store.load_edits(sid)[0]["status"] == "applied"
    with pytest.raises(AgentError, match="没有尚未撤销"):
        service(creation_chat).undo(sid)
    events = store.load_events(sid)
    store.close()
    with SessionStore(store.root) as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == events
        assert reopened.undo_checkpoint(sid, max_bytes=1024) is None
    assert not target.exists() and len(requests) == 2


def test_declining_creation_undo_preserves_file_and_can_retry_explicitly(creation_chat):
    conversation, _, control, requests, _, target = creation_chat
    conversation.send("create")
    control["approved"] = False
    sid = conversation.session.session_id
    assert service(creation_chat).undo(sid).status == "denied"
    assert target.read_bytes() == b"created\n"
    control["approved"] = True
    assert service(creation_chat).undo(sid).status == "applied"
    assert not target.exists() and len(requests) == 2


@pytest.mark.parametrize("during_approval", [False, True])
def test_external_content_before_or_during_approval_is_not_deleted(creation_chat, during_approval):
    conversation, editor, _, requests, approvals, target = creation_chat
    conversation.send("create")
    sid = conversation.session.session_id

    def confirm(plan):
        assert isinstance(plan, DeletePlan) and plan.candidate is None
        approvals.append(plan)
        target.write_bytes(b"external change")
        return True

    editor.confirm = confirm
    if not during_approval:
        target.write_bytes(b"external change")
    with pytest.raises(EditConflictError):
        service(creation_chat).undo(sid)
    assert target.read_bytes() == b"external change"
    assert len(approvals) == (2 if during_approval else 1) and len(requests) == 2
    assert conversation.store.load_undos(sid)[0]["status"] == "conflict"


@pytest.mark.parametrize("boundary", ["undo_prepared", "undo_started", "undo_finished"])
def test_undo_save_failure_never_repeats_deletion_or_guesses_success(
    creation_chat, monkeypatch, boundary
):
    conversation, _, _, requests, _, target = creation_chat
    conversation.send("create")
    sid, store = conversation.session.session_id, conversation.store
    original = store.append_undo_event

    def save(session, kind, payload):
        if kind == boundary:
            raise StorageError("injected undo save failure")
        original(session, kind, payload)

    monkeypatch.setattr(store, "append_undo_event", save)
    with pytest.raises(EditLogError):
        service(creation_chat).undo(sid)
    assert target.exists() is (boundary != "undo_finished")
    store.close()
    monkeypatch.setattr(store, "append_undo_event", original)
    with SessionStore(store.root) as reopened:
        reopened.select_session(sid)
        assert reopened.load_undos(sid)[0]["status"] == (
            "unknown" if boundary == "undo_finished" else "interrupted"
        )
        if boundary == "undo_finished":
            with pytest.raises(StorageError):
                reopened.undo_checkpoint(sid, max_bytes=1024)
    assert target.exists() is (boundary != "undo_finished") and len(requests) == 2


def test_unlink_failure_is_recorded_without_removing_file(creation_chat, monkeypatch):
    conversation, _, _, requests, _, target = creation_chat
    conversation.send("create")
    real_unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == target:
            raise PermissionError("injected delete failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    sid = conversation.session.session_id
    with pytest.raises(ToolError, match="删除失败"):
        service(creation_chat).undo(sid)
    assert target.read_bytes() == b"created\n" and len(requests) == 2
    assert conversation.store.load_undos(sid)[0]["status"] == "failed"


def test_cancelled_approval_and_revoked_permission_preserve_created_file(creation_chat):
    conversation, editor, _, requests, _, target = creation_chat
    conversation.send("create")
    sid = conversation.session.session_id

    def cancel(_plan):
        raise KeyboardInterrupt

    editor.confirm = cancel
    with pytest.raises(KeyboardInterrupt):
        service(creation_chat).undo(sid)
    assert conversation.store.load_undos(sid)[0]["status"] == "interrupted"
    editor.policy.write_roots = ()
    with pytest.raises(ToolError, match="additional_write_dirs"):
        service(creation_chat).undo(sid)
    assert target.read_bytes() == b"created\n" and len(requests) == 2

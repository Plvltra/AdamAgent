"""B22.2.3.1：测试宿主注册创建入口，穿过真实执行器和JSONL；生产目录尚不开放。"""

import json
from hashlib import sha256

import httpx
import pytest

from adam.config.settings import LLMConfig, PermissionConfig, ToolLimits
from adam.contracts.errors import ApprovalDeniedError, EditLogError, StorageError
from adam.contracts.execution import ToolBudget, ToolCapability
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.registry import ToolRegistry
from tests.features.test_edit_tool_loop import tool_message
from tests.features.test_tool_loop import answer
from tests.support import build_conversation


@pytest.fixture
def creation_chat(tmp_path):
    requests, approvals = [], []
    control = {"approved": True, "content": "created\n"}

    def confirm(plan):
        approvals.append(plan)
        return control["approved"]

    editor = EditTools(PathPolicy(tmp_path, PermissionConfig()), ToolLimits(), confirm)

    def create(args, record):
        return editor.execute_plan(editor.prepare_create(args), record)

    registry = ToolRegistry(
        {
            "create_file": schema(
                "create a new file",
                {"path": ("string", "target"), "content": ("string", "text")},
                ["path", "content"],
            )
        },
        {"create_file": create},
        ToolLimits(),
        capabilities={"create_file": ToolCapability.EDIT_FILE},
    )

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = tool_message(
                "create_file", {"path": "new.txt", "content": control["content"]}, "create"
            )
            result = {
                "choices": [{"finish_reason": "tool_calls", "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        else:
            result = answer("finished")
        return httpx.Response(200, json=result)

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            "",
            registry,
        )
        yield conversation, editor, control, requests, approvals, tmp_path / "new.txt"


@pytest.mark.parametrize("content", ["", "中文\r\nlast"], ids=["empty", "utf8"])
def test_actual_creation_records_absence_checkpoint_and_survives_restart(creation_chat, content):
    conversation, _, control, requests, approvals, target = creation_chat
    control["content"] = content
    assert conversation.send("create") == "finished"
    sid, store = conversation.session.session_id, conversation.store
    assert target.read_bytes() == content.replace("\r\n", "\n").encode()
    assert len(requests) == 2 and len(approvals) == 1
    edit = store.load_edits(sid)[0]
    assert edit["operation"] == "create" and edit["before_version"] is None
    assert (
        edit["status"] == "applied"
        and edit["after_version"] == sha256(target.read_bytes()).hexdigest()
    )
    checkpoint = store.load_edit_checkpoint(sid, edit["edit_id"], max_bytes=1024)
    assert checkpoint.operation == "create" and checkpoint.original is None
    assert checkpoint.before_version is None and checkpoint.tool_call_id == "create"
    assert store.execution_report(sid, edit["turn_id"]).actions[0].kind == "create_file"
    events = store.load_events(sid)
    store.close()
    with SessionStore(store.root) as reopened:
        assert reopened.load_edit_checkpoint(sid, edit["edit_id"], max_bytes=1024) == checkpoint
        assert reopened.load_events(sid) == events
    assert len(requests) == 2


def test_creation_denied_has_no_checkpoint_or_file(creation_chat):
    conversation, _, control, requests, approvals, target = creation_chat
    control["approved"] = False
    with pytest.raises(ApprovalDeniedError):
        conversation.send("create")
    store, sid = conversation.store, conversation.session.session_id
    assert len(approvals) == 1 and len(requests) == 1 and not target.exists()
    assert store.load_edits(sid)[0]["status"] == "denied"
    assert not list((store.root / "_checkpoints").rglob("*.json"))
    assert store.load_messages(sid) == []


@pytest.mark.parametrize(
    "boundary", ["edit_planned", "checkpoint", "edit_started", "edit_finished"]
)
def test_save_failure_stops_and_restart_never_recreates_or_upgrades(
    creation_chat, monkeypatch, boundary
):
    conversation, _, _, requests, _, target = creation_chat
    store = conversation.store
    original_event, original_checkpoint = store.append_edit_event, store.save_edit_checkpoint

    def event(session, kind, payload):
        if kind == boundary:
            raise StorageError("injected event save failure")
        original_event(session, kind, payload)

    def checkpoint(*args, **kwargs):
        raise StorageError("injected checkpoint save failure")

    monkeypatch.setattr(store, "append_edit_event", event)
    if boundary == "checkpoint":
        monkeypatch.setattr(store, "save_edit_checkpoint", checkpoint)
    with pytest.raises(EditLogError):
        conversation.send("create")
    assert len(requests) == 1 and target.exists() is (boundary == "edit_finished")
    sid = conversation.session.session_id
    store.close()
    monkeypatch.setattr(store, "append_edit_event", original_event)
    monkeypatch.setattr(store, "save_edit_checkpoint", original_checkpoint)
    conversation.resume(sid)
    edits = store.load_edits(sid)
    assert store.load_messages(sid) == []
    if boundary == "edit_planned":
        assert edits == []
    else:
        assert edits[0]["status"] == ("unknown" if boundary == "edit_finished" else "not_started")
        with pytest.raises(StorageError, match="未确认成功"):
            store.load_edit_checkpoint(sid, edits[0]["edit_id"], max_bytes=1024)
    if boundary == "edit_finished":
        with pytest.raises(StorageError):
            conversation.send("do not retry")
        assert target.read_bytes() == b"created\n"
    assert len(requests) == 1


def test_last_moment_collision_is_logged_without_overwrite(creation_chat, monkeypatch):
    import adam.tool_calls.edit_tools as edits

    conversation, _, _, requests, _, target = creation_chat
    real_link = edits.os.link

    def race(source, destination):
        destination.write_bytes(b"external")
        real_link(source, destination)

    monkeypatch.setattr(edits.os, "link", race)
    assert conversation.send("create") == "finished"
    edit = conversation.store.load_edits(conversation.session.session_id)[0]
    assert edit["status"] == "conflict" and edit["after_version"] is None
    assert target.read_bytes() == b"external" and len(requests) == 2
    output = json.loads(requests[-1]["messages"][-1]["content"])
    assert output["ok"] is False and output["status"] == "conflict"


def test_creation_counts_as_edit_attempt_and_is_not_exposed_by_production_factory(tmp_path):
    from adam.contracts.errors import BudgetError

    budget = ToolBudget(max_edit_attempts=1)
    budget.consume("create_file")
    assert budget.edit_attempts == 1
    with pytest.raises(BudgetError):
        budget.consume("replace_text")
    assert all(
        item["function"]["name"] != "create_file"
        for item in create_tool_registry(tmp_path).definitions()
    )

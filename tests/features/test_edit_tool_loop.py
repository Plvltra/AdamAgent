"""模型文件编辑闭环与故障留痕：假 HTTP，真实文件和会话存储。"""

import json

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import ApprovalDeniedError, EditLogError, StorageError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def tool_message(name, args, call_id):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


@pytest.fixture
def edit_chat(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_bytes(b"old\n")
    requests = []
    control = {"approve": True, "extra_call": False, "expected_status": "applied"}

    def confirm(plan):
        if callback := control.get("before_approval"):
            callback(plan)
        return control["approve"]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = tool_message("read_file", {"path": "sample.txt"}, "read")
        elif len(requests) == 2:
            version = json.loads(body["messages"][-1]["content"])["version"]
            message = tool_message(
                "replace_text",
                {
                    "path": "sample.txt",
                    "old_text": "old",
                    "new_text": "new",
                    "expected_version": version,
                },
                "edit",
            )
            if control["extra_call"]:
                message["tool_calls"] += tool_message("read_file", {"path": "sample.txt"}, "later")[
                    "tool_calls"
                ]
        else:
            assert (
                json.loads(body["messages"][-1]["content"])["status"] == control["expected_status"]
            )
            message = {"role": "assistant", "content": "edited"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            "",
            create_tool_registry(tmp_path, confirm_edit=confirm),
        )
        yield conversation, target, requests, control


def test_model_edit_is_linked_and_survives_restart(edit_chat):
    conversation, target, requests, _ = edit_chat
    assert conversation.send("change old to new") == "edited"
    session_id = conversation.session.session_id
    assert target.read_bytes() == b"new\n" and len(requests) == 3
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    turn = report["turns"][0]
    record = turn["edits"][0]
    assert record["status"] == "applied" and record["tool_call_id"] == "edit"
    assert record["request_id"] == turn["requests"][1]["request_id"]
    assert record["before_version"] != record["after_version"] and record["duration_ms"] >= 0
    conversation.store.close()
    with SessionStore(conversation.store.root) as reopened:
        reopened.select_session(session_id)
        assert reopened.load_edits(session_id)[0]["status"] == "applied"
        assert len(reopened.load_messages(session_id)) == 6


def test_denial_stops_remaining_batch_and_model_requests(edit_chat):
    conversation, target, requests, control = edit_chat
    control.update(approve=False, extra_call=True)
    with pytest.raises(ApprovalDeniedError):
        conversation.send("change")
    assert target.read_bytes() == b"old\n" and len(requests) == 2
    report = read_report(next(conversation.store.root.rglob("*.jsonl")))
    turn = report["turns"][0]
    assert [item["tool_call_id"] for item in turn["tools"]] == ["read", "edit"]
    assert turn["edits"][0]["status"] == "denied" and turn["reason"] == "approval_denied"
    assert conversation.store.load_messages(conversation.session.session_id) == []


@pytest.mark.parametrize(
    "boundary,changed", [("edit_planned", False), ("edit_started", False), ("edit_finished", True)]
)
def test_edit_log_failure_stops_without_retry(edit_chat, monkeypatch, boundary, changed):
    conversation, target, requests, _ = edit_chat
    original = conversation.store.append_edit_event

    def fail(session_id, kind, payload):
        if kind == boundary:
            raise StorageError("injected edit log failure")
        original(session_id, kind, payload)

    monkeypatch.setattr(conversation.store, "append_edit_event", fail)
    with pytest.raises(EditLogError):
        conversation.send("change")
    assert target.read_bytes() == (b"new\n" if changed else b"old\n")
    assert len(requests) == 2
    session_id = conversation.session.session_id
    conversation.store.close()
    monkeypatch.setattr(conversation.store, "append_edit_event", original)
    conversation.resume(session_id)
    if changed:
        assert conversation.store.load_edits(session_id)[0]["status"] == "unknown"
        with pytest.raises(StorageError) as caught:
            conversation.send("do not repeat")
        assert caught.value.code == "execution_unknown"
        with pytest.raises(StorageError):
            conversation.compact()
        assert len(requests) == 2
    elif boundary == "edit_started":
        assert conversation.store.load_edits(session_id)[0]["status"] == "not_started"


@pytest.mark.parametrize(
    "field,value", [("after_version", "0" * 64), ("path", "other"), ("request_id", "wrong")]
)
def test_edit_finish_rejects_forged_source_or_version(edit_chat, monkeypatch, field, value):
    conversation, target, requests, _ = edit_chat
    original = conversation.store.append_edit_event

    def corrupt(session_id, kind, payload):
        original(
            session_id, kind, {**payload, **({field: value} if kind == "edit_finished" else {})}
        )

    monkeypatch.setattr(conversation.store, "append_edit_event", corrupt)
    with pytest.raises(EditLogError):
        conversation.send("change")
    assert target.read_bytes() == b"new\n" and len(requests) == 2
    events = conversation.store.load_events(conversation.session.session_id)
    assert not any(event["type"] == "edit_finished" for event in events)
    assert conversation.store.load_edits(conversation.session.session_id)[0]["status"] == "unknown"


def test_conflict_is_recorded_and_returned_to_model(edit_chat):
    conversation, target, requests, control = edit_chat

    def change(plan):
        target.write_bytes(b"external")
        return True

    control["before_approval"] = change
    control["expected_status"] = "conflict"
    conversation.send("change")
    result = json.loads(requests[-1]["messages"][-1]["content"])
    assert result["status"] == "conflict" and result["ok"] is False
    assert target.read_bytes() == b"external"
    record = conversation.store.load_edits(conversation.session.session_id)[0]
    assert record["status"] == "conflict" and record["after_version"] is None

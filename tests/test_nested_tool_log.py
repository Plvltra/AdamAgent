"""B14.2.1：父子来源与固定请求名单的边界，不调用隔离进程或模型。"""

import json
from pathlib import Path

import pytest

from adam.contracts.execution import CODE_TOOL_NAME, ToolBudget, ToolCapability
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry


@pytest.fixture
def store_turn(tmp_path):
    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("nested", "")
        turn = store.start_turn(sid, "test")
        request = store.start_request(sid, turn, "fake-model")
        store.finish_request(sid, turn, request, (1, 1))
        yield store, sid, {"turn_id": turn, "request_id": request}


def start(store, sid, source, call_id, *, name=CODE_TOOL_NAME, parent=None):
    store.append_tool_event(
        sid,
        "tool_started",
        {
            **source,
            "tool_call_id": call_id,
            "name": name,
            "arguments": "{}",
            **({"parent_tool_call_id": parent} if parent is not None else {}),
        },
    )


def finish(store, sid, source, call_id, *, parent=None):
    store.append_tool_event(
        sid,
        "tool_finished",
        {
            **source,
            "tool_call_id": call_id,
            "output": '{"ok":true}',
            **({"parent_tool_call_id": parent} if parent is not None else {}),
        },
    )


@pytest.mark.parametrize(
    "invalid", ["missing", "self", "different_request", "finished", "ordinary", "nested_parent"]
)
def test_invalid_parent_rejected_before_append(store_turn, invalid):
    store, sid, source = store_turn
    start(store, sid, source, "root", name="read_file" if invalid == "ordinary" else CODE_TOOL_NAME)
    parent = "root"
    if invalid == "finished":
        finish(store, sid, source, "root")
    elif invalid == "different_request":
        request = store.start_request(sid, source["turn_id"], "second")
        store.finish_request(sid, source["turn_id"], request, (1, 1))
        source = {**source, "request_id": request}
    elif invalid == "nested_parent":
        start(store, sid, source, "child", parent="root")
        parent = "child"
    elif invalid in {"missing", "self"}:
        parent = "unknown" if invalid == "missing" else "new"
    before = store.load_events(sid)
    with pytest.raises(ValueError, match="nested tool parent"):
        start(store, sid, source, "new", parent=parent)
    assert store.load_events(sid) == before


def test_parent_cannot_finish_before_child_or_reassign_its_parent(store_turn):
    store, sid, source = store_turn
    start(store, sid, source, "root")
    start(store, sid, source, "child", name="read_file", parent="root")
    with pytest.raises(ValueError, match="unfinished nested"):
        finish(store, sid, source, "root")
    for parent in (None, "another"):
        with pytest.raises(ValueError, match="mismatched nested"):
            finish(store, sid, source, "child", parent=parent)
    finish(store, sid, source, "child", parent="root")
    finish(store, sid, source, "root")


def test_nested_result_is_not_an_independent_model_tool_message(store_turn):
    store, sid, source = store_turn
    start(store, sid, source, "root")
    start(store, sid, source, "child", name="read_file", parent="root")
    finish(store, sid, source, "child", parent="root")
    finish(store, sid, source, "root")
    calls = [
        {"id": name, "type": "function", "function": {"name": tool, "arguments": "{}"}}
        for name, tool in (("root", CODE_TOOL_NAME), ("child", "read_file"))
    ]
    messages = [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *[
            {"role": "tool", "tool_call_id": name, "content": '{"ok":true}'}
            for name in ("root", "child")
        ],
        {"role": "assistant", "content": "done"},
    ]
    with pytest.raises(ValueError, match="mismatched tool request"):
        store.append_messages(sid, source["turn_id"], messages)
    assert store.load_messages(sid) == []


def test_nested_visibility_is_frozen_and_rejected_attempt_still_counts(
    tmp_path, store_turn, monkeypatch
):
    store, sid, source = store_turn
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    original = Path.open

    def deny_read(path, *args, **kwargs):
        if path == tmp_path / "a.txt":
            pytest.fail("hidden tool read file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_read)
    registry = create_tool_registry(tmp_path).extended(
        {CODE_TOOL_NAME: schema("test orchestration", {}, [])},
        {
            CODE_TOOL_NAME: lambda args, record: executor.execute_nested(
                "read_file", {"path": "a.txt"}
            )
        },
        capabilities={CODE_TOOL_NAME: ToolCapability.DELEGATE},
    )
    executor = ToolExecutor(store, registry, lambda _: None)
    budget = ToolBudget(3, max_tool_calls=2)
    calls = [{"id": "parent", "function": {"name": CODE_TOOL_NAME, "arguments": "{}"}}]
    messages = executor.execute(
        sid,
        source["turn_id"],
        source["request_id"],
        calls,
        budget,
        provided_names=(CODE_TOOL_NAME,),
    )
    output = json.loads(messages[0]["content"])
    assert not output["ok"] and "本次请求尚未提供" in output["error"]
    assert budget.tool_calls == 2 and budget.edit_attempts == 0
    events = store.load_events(sid)
    assert sum(e["type"] == "tool_started" for e in events) == 2

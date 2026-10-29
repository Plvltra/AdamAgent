"""E3.1：计划版本、同轮归属、失败保护和重启读取，均使用临时真实 JSONL。"""

import json
import os
from dataclasses import replace

import pytest

from adam.contracts.errors import StorageError
from adam.contracts.planning import PlanSnapshot, PlanStep
from adam.session.storage import SessionStore


def begin_tool(store, session_id, turn_id, request_id, call_id, name="update_plan"):
    """生成真实工具父事件，避免直接伪造无来源的计划记录。"""
    store.append_tool_event(
        session_id,
        "tool_started",
        {
            "turn_id": turn_id,
            "request_id": request_id,
            "tool_call_id": call_id,
            "name": name,
            "arguments": "{}",
        },
    )


def finish_tool(store, session_id, turn_id, request_id, call_id):
    store.append_tool_event(
        session_id,
        "tool_finished",
        {
            "turn_id": turn_id,
            "request_id": request_id,
            "tool_call_id": call_id,
            "output": '{"ok":true}',
        },
    )


def start_plan(store):
    session_id = store.create_session("plan test", "constraint")
    store.select_session(session_id)
    turn_id = store.start_turn(session_id, "change two files")
    request_id = store.start_request(session_id, turn_id, "test")
    store.finish_request(session_id, turn_id, request_id, (1, 1))
    begin_tool(store, session_id, turn_id, request_id, "p1")
    plan = PlanSnapshot(
        turn_id,
        request_id,
        "p1",
        1,
        (
            PlanStep("read", "read two files", "in_progress"),
            PlanStep("edit", "edit both files", "pending"),
        ),
    )
    return session_id, plan


def test_plan_versions_survive_restart_without_committing_chat_or_rerunning(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id, plan = start_plan(store)
        store.append_plan(session_id, plan)
        finish_tool(store, session_id, plan.turn_id, plan.request_id, "p1")
        begin_tool(store, session_id, plan.turn_id, plan.request_id, "p2")
        changed = replace(
            plan,
            version=2,
            tool_call_id="p2",
            reason="second file unavailable",
            replan_count=1,
            steps=(
                PlanStep("read", "read one file", "failed", reason="missing file"),
                PlanStep("edit", "edit available file", "pending"),
            ),
        )
        store.append_plan(session_id, changed)
        assert store.load_messages(session_id) == []
    with SessionStore(tmp_path) as reopened:
        reopened.select_session(session_id)
        report = reopened.load_plan(session_id)
        assert report.snapshot == changed
        assert report.turn_status == "interrupted"
        assert report.turn_reason == "restart_recovery"
        assert [step.status for step in report.snapshot.steps] == ["failed", "pending"]
        events = reopened.load_events(session_id)
        assert len([event for event in events if event["type"] == "request_started"]) == 1
        versions = [e["payload"]["version"] for e in events if e["type"] == "plan_updated"]
        assert versions == [1, 2]
        assert reopened.load_messages(session_id) == []
        other = reopened.create_session("isolated", "")
        reopened.select_session(other)
        assert reopened.load_plan(other) is None


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"max_steps": True},
        {"replan_count": 1},
        {"steps": (PlanStep("same", "first", "pending"), PlanStep("same", "second", "pending"))},
        {"steps": (PlanStep("a", "first", "in_progress"), PlanStep("b", "second", "in_progress"))},
        {"steps": (PlanStep("a", "unfinished", "completed"),)},
    ],
)
def test_invalid_plan_never_changes_existing_bytes(tmp_path, change):
    with SessionStore(tmp_path) as store:
        session_id, plan = start_plan(store)
        path = next(tmp_path.rglob(f"{session_id}.jsonl"))
        before = path.read_bytes()
        with pytest.raises(ValueError):
            store.append_plan(session_id, replace(plan, **change))
        assert path.read_bytes() == before and store.load_plan(session_id) is None


def test_replan_limit_and_tool_source_are_checked_before_append(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id, plan = start_plan(store)
        plan = replace(plan, max_replans=0)
        store.append_plan(session_id, plan)
        finish_tool(store, session_id, plan.turn_id, plan.request_id, "p1")
        begin_tool(store, session_id, plan.turn_id, plan.request_id, "p2")
        changed = replace(
            plan,
            tool_call_id="p2",
            version=2,
            replan_count=1,
            reason="change",
            steps=(PlanStep("other", "new work", "pending"),),
        )
        with pytest.raises(ValueError, match="limit"):
            store.append_plan(session_id, changed)
        begin_tool(store, session_id, plan.turn_id, plan.request_id, "read", "read_file")
        with pytest.raises(ValueError, match="parent"):
            store.append_plan(session_id, replace(plan, tool_call_id="read", version=2))
        assert store.load_plan(session_id).snapshot == plan


def test_plan_fsync_failure_rolls_back_to_last_valid_version(tmp_path, monkeypatch):
    with SessionStore(tmp_path) as store:
        session_id, plan = start_plan(store)
        store.append_plan(session_id, plan)
        finish_tool(store, session_id, plan.turn_id, plan.request_id, "p1")
        begin_tool(store, session_id, plan.turn_id, plan.request_id, "p2")
        path = next(tmp_path.rglob(f"{session_id}.jsonl"))
        before = path.read_bytes()
        original = os.fsync
        failed = False

        def fail_once(fd):
            # 注入计划追加后的同步故障，回滚同步仍使用真实 fsync。
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected plan fsync failure")
            return original(fd)

        monkeypatch.setattr(os, "fsync", fail_once)
        with pytest.raises(StorageError):
            store.append_plan(session_id, replace(plan, tool_call_id="p2", version=2))
        assert path.read_bytes() == before
        assert store.load_plan(session_id).snapshot == plan
        assert (
            json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["type"] == "tool_started"
        )

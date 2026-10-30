"""Hook 日志状态与关联校验；真实工具/进程场景放在 features/test_tool_hooks.py。"""

import json
from contextlib import contextmanager

import pytest

from adam.contracts.hooks import HookResult
from adam.session.storage import SessionStore


@contextmanager
def started_tool(root):
    """建立合法请求和工具来源，后续用公开存储接口验证非法追加被原子拒绝。"""
    with SessionStore(root) as store:
        sid = store.create_session("hooks", "")
        turn = store.start_turn(sid, "read")
        request = store.start_request(sid, turn, "test")
        store.finish_request(sid, turn, request, (1, 1))
        link = {"turn_id": turn, "request_id": request, "tool_call_id": "read"}
        store.append_tool_event(
            sid,
            "tool_started",
            {
                **link,
                "name": "read_file",
                "arguments": '{"path":"a.txt"}',
            },
        )
        hook = {**link, "hook_id": "h1", "event": "before_tool", "execution_mode": "act"}
        yield store, sid, link, hook


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "missing"},
        {"turn_id": "missing"},
        {"tool_call_id": "missing"},
        {"event": "after_tool"},
        {"event": "unknown"},
        {"execution_mode": "admin"},
        {"unexpected": True},
    ],
)
def test_hook_start_rejects_wrong_source_stage_or_fields(tmp_path, change):
    with started_tool(tmp_path) as (store, sid, _, hook):
        before = store.load_events(sid)
        with pytest.raises(ValueError):
            store.append_hook_event(sid, "hook_started", {**hook, **change})
        assert store.load_events(sid) == before


@pytest.mark.parametrize(
    "change",
    [
        {"status": "observed"},
        {"status": []},
        {"duration_ms": True},
        {"duration_ms": -1},
        {"error_code": "timed_out"},
        {"exit_code": 3},
        {"message": 3},
        {"stderr": "x" * 65537},
    ],
)
def test_hook_finish_rejects_inconsistent_results(tmp_path, change):
    with started_tool(tmp_path) as (store, sid, _, hook):
        store.append_hook_event(sid, "hook_started", hook)
        before = store.load_events(sid)
        result = {**HookResult("allowed", exit_code=0).to_record(), **change}
        with pytest.raises(ValueError):
            store.append_hook_event(sid, "hook_finished", {**hook, "result": result})
        assert store.load_events(sid) == before


def test_pending_and_blocked_before_hooks_cannot_claim_tool_success(tmp_path):
    with started_tool(tmp_path) as (store, sid, link, hook):
        store.append_hook_event(sid, "hook_started", hook)
        with pytest.raises(ValueError, match="before hook"):
            store.append_tool_event(sid, "tool_finished", {**link, "output": '{"ok":true}'})
        finish = {**hook, "result": HookResult("denied", exit_code=0).to_record()}
        store.append_hook_event(sid, "hook_finished", finish)
        with pytest.raises(ValueError, match="blocked hook"):
            store.append_tool_event(sid, "tool_finished", {**link, "output": '{"ok":true}'})
        with pytest.raises(ValueError, match="duplicate"):
            store.append_hook_event(sid, "hook_finished", finish)
        store.append_tool_event(
            sid,
            "tool_finished",
            {
                **link,
                "output": '{"ok":false,"status":"hook_blocked"}',
            },
        )
        with pytest.raises(ValueError, match="allowed before"):
            store.append_hook_event(
                sid, "hook_started", {**hook, "hook_id": "h2", "event": "after_tool"}
            )


def test_pending_after_hook_prevents_success_commit_and_recovers_without_losing_result(tmp_path):
    with started_tool(tmp_path) as (store, sid, link, hook):
        output = '{"ok":true,"content":"actual evidence"}'
        store.append_tool_event(sid, "tool_finished", {**link, "output": output})
        store.append_hook_event(sid, "hook_started", {**hook, "event": "after_tool"})
        messages = [
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read", "content": output},
            {"role": "assistant", "content": "done"},
        ]
        with pytest.raises(ValueError, match="unfinished hook"):
            store.append_messages(sid, link["turn_id"], messages)
        assert store.load_messages(sid) == []
    with SessionStore(tmp_path) as reopened:
        reopened.select_session(sid)
        assert reopened.load_hooks(sid)[0]["status"] == "interrupted"
        assert reopened.load_hooks(sid)[0]["duration_ms"] is None
        result = next(e for e in reopened.load_events(sid) if e["type"] == "tool_finished")
        assert json.loads(result["payload"]["output"])["content"] == "actual evidence"
        assert reopened.load_messages(sid) == []

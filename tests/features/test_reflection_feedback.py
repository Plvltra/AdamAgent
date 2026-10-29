"""E4.1：反馈保存校验真实关联，失败输入隔离，重启只读恢复。"""

from dataclasses import replace

import pytest

from adam.contracts.reflection import Feedback
from adam.session.storage import SessionStore
from tests.features.test_task_plan_storage import begin_tool


def feedback_turn(store, kind="tool"):
    """构造可回放请求/工具事实；远端和执行过程在后续服务测试中覆盖。"""
    session_id = store.create_session("feedback", "")
    store.select_session(session_id)
    store.append_turn(session_id, "previous input", "previous reply")
    turn_id = store.start_turn(session_id, "你把路径算错了，应以配置文件目录为基准")
    request_id = store.start_request(session_id, turn_id, "test")
    store.finish_request(session_id, turn_id, request_id, (10, 2))
    begin_tool(
        store,
        session_id,
        turn_id,
        request_id,
        "work",
        "read_file" if kind == "tool" else "report_feedback",
    )
    source = store.feedback_sources(session_id, turn_id)["previous"]["turn_id"]
    if kind == "tool":
        store.append_tool_event(
            session_id,
            "tool_finished",
            {
                "turn_id": turn_id,
                "request_id": request_id,
                "tool_call_id": "work",
                "output": '{"ok":false,"error":"missing file"}',
            },
        )
    return session_id, Feedback(
        "f1",
        turn_id,
        turn_id if kind == "tool" else source,
        request_id,
        "work",
        kind,
        "" if kind == "tool" else "你把路径算错了",
    )


@pytest.mark.parametrize("kind", ["tool", "user"])
def test_feedback_survives_restart_without_replaying_or_committing_failed_input(tmp_path, kind):
    with SessionStore(tmp_path) as store:
        session_id, feedback = feedback_turn(store, kind)
        store.append_feedback(session_id, feedback)
        facts = store.feedback_context(session_id, feedback.feedback_id)
        assert facts["source_input"] == (
            "previous input" if kind == "user" else "你把路径算错了，应以配置文件目录为基准"
        )
        assert len(store.load_messages(session_id)) == 2
        assert store.load_feedback(session_id, feedback.turn_id) == [feedback]
    with SessionStore(tmp_path) as reopened:
        reopened.select_session(session_id)
        assert reopened.load_feedback(session_id, feedback.turn_id) == [feedback]
        assert len(reopened.load_messages(session_id)) == 2
        assert sum(e["type"] == "request_started" for e in reopened.load_events(session_id)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"source_turn_id": "foreign"},
        {"request_id": "invented"},
        {"tool_call_id": "invented"},
        {"quote": "not user input"},
    ],
)
def test_invalid_correction_never_appends(tmp_path, change):
    with SessionStore(tmp_path) as store:
        session_id, feedback = feedback_turn(store, "user")
        before = store.load_events(session_id)
        with pytest.raises(ValueError):
            store.append_feedback(session_id, replace(feedback, **change))
        assert store.load_events(session_id) == before


def test_duplicate_feedback_and_success_as_failure_are_rejected(tmp_path):
    with SessionStore(tmp_path) as store:
        session_id, feedback = feedback_turn(store)
        store.append_feedback(session_id, feedback)
        with pytest.raises(ValueError, match="duplicate"):
            store.append_feedback(session_id, replace(feedback, feedback_id="f2"))
        begin_tool(store, session_id, feedback.turn_id, feedback.request_id, "success", "read_file")
        store.append_tool_event(
            session_id,
            "tool_finished",
            {
                "turn_id": feedback.turn_id,
                "request_id": feedback.request_id,
                "tool_call_id": "success",
                "output": '{"ok":true,"content":"real"}',
            },
        )
        with pytest.raises(ValueError, match="failed tool"):
            store.append_feedback(
                session_id, replace(feedback, feedback_id="f3", tool_call_id="success")
            )

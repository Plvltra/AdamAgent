"""B03.2：真实计划工具闭环、日志和旧新布局重放；HTTP 使用模拟，输入来源真实保存。"""

import json

import httpx
import pytest

from adam.context.builder import ContextBuilder
from adam.contracts.errors import AgentError
from adam.contracts.history import CONTEXT_LAYOUT_VERSION
from adam.recording.format import RecordingError
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from tests.features.test_independent_review import reviewer
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import step
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


@pytest.mark.parametrize("layout", [None, CONTEXT_LAYOUT_VERSION])
def test_plan_reminder_keeps_protocol_and_selected_layout_through_log_and_replay(
    tmp_path, monkeypatch, layout
):
    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call("read", "read_file", path="note.txt"),
                ]
            )
        elif number == 2:
            value = answer("premature")
        elif number == 3:
            reminder = next(
                m for m in body["messages"] if "计划收尾提醒" in (m.get("content") or "")
            )
            assert reminder["role"] == ("system" if layout is None else "user")
            value = tool_response(
                [call("done", "update_plan", steps=[step("completed", ["read"])])]
            )
        else:
            value = answer("verified")
        return httpx.Response(200, json=value)

    with monkeypatch.context() as patch:
        # 旧录制由旧排列分支真实生成，不能仅删字段冒充旧请求。
        patch.setattr(
            "adam.application.conversation.ContextBuilder",
            lambda limits, output, **_: ContextBuilder(limits, output, layout_version=layout),
        )
        with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
            assert recorder.run(lambda: app.send("task")) == "verified"
            events = store.load_events(app.session.session_id)
            requests = [e["payload"] for e in events if e["type"] == "request_started"]
            assert len(requests) == 4 and all(
                r["context"].get("layout_version") == layout for r in requests
            )
            assert "premature" not in str([m.raw for m in app.history()])
            if layout is not None:
                assert bodies[0]["messages"][0] == bodies[2]["messages"][0]
                assert "计划收尾提醒" not in bodies[2]["messages"][0]["content"]
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"
    replayed = read_report(next((tmp_path / "offline").rglob("*.jsonl")))
    assert all(
        r["context"].get("layout_version") == layout for r in replayed["turns"][-1]["requests"]
    )


@pytest.mark.parametrize("target", ["log", "recording"])
def test_unknown_layout_is_rejected_without_rewriting_original_or_requesting_model(
    tmp_path, monkeypatch, target
):
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("ok"))) as (
        app,
        recorder,
        store,
        source,
        _,
    ):
        recorder.run(lambda: app.send("task"))
        if target == "log":
            events = store.load_events(app.session.session_id)
            session_id = app.session.session_id
            next(e["payload"]["context"] for e in events if e["type"] == "request_started")[
                "layout_version"
            ] = "future"
        else:
            events = [json.loads(line) for line in source.read_bytes().splitlines()]
            events[1]["data"]["value"]["value"]["context_layout"] = "future"
    damaged = tmp_path / (f"{session_id}.jsonl" if target == "log" else "damaged.jsonl")
    damaged.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    before = damaged.read_bytes()
    forbid_external_actions(monkeypatch)
    with pytest.raises((AgentError, RecordingError)):
        if target == "log":
            read_report(damaged)
        else:
            replay_turn(damaged, tmp_path / "offline")
    assert damaged.read_bytes() == before


def test_actual_review_report_is_reference_and_does_not_change_system_prefix(tmp_path, monkeypatch):
    (tmp_path / "code.py").write_text("value = 5\n", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call(
                        "review",
                        "request_review",
                        goal="确认数值",
                        criteria="value == 5",
                        paths=["code.py"],
                    )
                ]
            )
        elif number == 2:
            value = tool_response(
                [call("collect", "collect_review", review_id=outputs(body)[-1]["review_id"])]
            )
        else:
            assert all(
                "当前审查" not in (m.get("content") or "")
                for m in body["messages"]
                if m["role"] == "system"
            )
            assert any(
                m["role"] == "user" and "当前审查" in (m.get("content") or "")
                for m in body["messages"]
            )
            value = answer("已收集审查")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, child_handler=reviewer) as (
        app,
        recorder,
        _,
        source,
        bodies,
    ):
        assert recorder.run(lambda: app.send("task")) == "已收集审查"
        assert len(bodies) == 3 and bodies[0]["messages"][0] == bodies[-1]["messages"][0]
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"

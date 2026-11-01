"""B04.2：程序状态进入实际请求与日志；HTTP 可控，工具、存储和离线重放真实执行。"""

import json

import httpx
import pytest

from adam.application.runtime_state import RuntimeStateReader
from adam.cli.terminal import show_context
from adam.contracts.errors import AgentError, StorageError
from adam.contracts.runtime import decode_runtime_state
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def view(body):
    """解析本场景中的唯一状态资料，不从模型普通回答提取程序事实。"""
    messages = [m for m in body["messages"] if "程序状态快照：" in (m.get("content") or "")]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    return json.loads(messages[0]["content"].rsplit("\n", 1)[1])


def test_retry_and_tool_failure_keep_exact_snapshot_in_input_log_and_replay(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("real evidence", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            raise httpx.ConnectError("synthetic pre-send failure")
        if number in (2, 3):
            value = tool_response(
                [call(f"read-{number}", "read_file", path="missing" if number == 2 else "note.txt")]
            )
        else:
            value = answer("done")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "done"
        states = [view(body) for body in bodies]
        assert [s["requests"]["used"] for s in states] == [0, 0, 2, 3]
        assert [s["tools"]["used"] for s in states] == [0, 0, 1, 2]
        assert [s["failures"]["consecutive"] for s in states] == [0, 0, 1, 0]
        assert [s["tokens"]["known_used"] for s in states] == [0, 0, 12, 24]
        assert bodies[0]["messages"] == bodies[1]["messages"]
        assert all(body["messages"][0] == bodies[0]["messages"][0] for body in bodies)
        events = store.load_events(app.session.session_id)
        contexts = [e["payload"]["context"] for e in events if e["type"] == "request_started"]
        assert len(contexts) == 4
        for context, body in zip(contexts, bodies, strict=True):
            state = decode_runtime_state(context["runtime_state"])
            assert any(state.text() in (m.get("content") or "") for m in body["messages"])
        assert all("程序状态快照" not in str(m.raw) for m in app.history())
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "real evidence"
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"
    replayed = read_report(next((tmp_path / "offline").rglob("*.jsonl")))
    assert [r["context"] for r in replayed["turns"][-1]["requests"]] == contexts


def test_base_preview_does_not_query_or_mutate_live_state(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("基础预览访问了实际轮次状态或远端")

    monkeypatch.setattr(RuntimeStateReader, "__call__", forbidden)
    with captured_app(tmp_path, forbidden) as (app, _, store, _, bodies):
        before = store.load_events(app.session.session_id)
        context = app.preview_context("hypothetical")
        assert context.runtime_state is None and bodies == []
        assert store.load_events(app.session.session_id) == before
        lines = []
        show_context(context, lines.append)
        assert any("未包含执行时的状态快照" in line for line in lines)


def test_state_source_failure_stops_before_model_and_keeps_failed_input(tmp_path, monkeypatch):
    failure = StorageError("synthetic snapshot read failure")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(RuntimeStateReader, "__call__", fail)
    with captured_app(tmp_path, lambda *_: pytest.fail("snapshot failure sent request")) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):
        with pytest.raises(StorageError) as caught:
            recorder.run(lambda: app.send("task"))
        assert caught.value is failure and bodies == []
        events = store.load_events(app.session.session_id)
        assert not any(e["type"] == "request_started" for e in events)
        assert app.execution_report().status == "failed"
        assert all(m.raw.get("content") != "task" for m in app.history())


def test_previous_stable_layout_recording_replays_without_inventing_state(tmp_path, monkeypatch):
    # 通过旧行为真实录制；不能删除新记录字段冒充当时没有发生过的状态查询。
    with monkeypatch.context() as patch:
        patch.setattr("adam.bootstrap.RuntimeStateReader", lambda *args, **kwargs: None)
        with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("ok"))) as (
            app,
            recorder,
            _,
            source,
            bodies,
        ):
            recorder.run(lambda: app.send("task"))
            assert all("程序状态快照：" not in str(body) for body in bodies)
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


def test_tampered_derived_remaining_is_rejected_without_repairing_log(tmp_path):
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("ok"))) as (
        app,
        recorder,
        store,
        _,
        _,
    ):
        recorder.run(lambda: app.send("task"))
        events = store.load_events(app.session.session_id)
        state = next(
            e["payload"]["context"]["runtime_state"]
            for e in events
            if e["type"] == "request_started"
        )
        state["tokens"]["remaining"] = 0
    path = tmp_path / "damaged.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AgentError):
        read_report(path)
    assert path.read_bytes() == before

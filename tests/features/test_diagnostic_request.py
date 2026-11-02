"""B20.2.2：诊断实际进入模型请求与日志；编辑事实、历史及离线环境保持独立。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.application.diagnostics import DiagnosticContextReader
from adam.config.settings import ContextConfig
from adam.contracts.diagnostic_context import decode_selection
from adam.contracts.errors import BudgetError, StorageError
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.diagnostic_files import DiagnosticFiles
from tests.features.test_diagnostic_selection import batch_editor
from tests.features.test_edit_diagnostics import structured_hooks
from tests.features.test_hook_acceptance import edit_model
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import outputs
from tests.features.test_tool_hooks import hooked_app
from tests.features.test_tool_loop import answer
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def observations(body):
    """只读程序独立注入的诊断，不从模型回答或原工具输出猜测状态。"""
    return [
        json.loads(message["content"].split("\n", 1)[1])
        for message in body["messages"]
        if (message.get("content") or "").startswith("以下是程序提供的本轮修改后检查观察")
    ]


def request_contexts(store, sid):
    return [
        e["payload"]["context"] for e in store.load_events(sid) if e["type"] == "request_started"
    ]


def test_bad_edit_then_repair_has_exact_request_evidence_and_zero_io_replay(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(tmp_path, batch_editor([target], repair=True), config=config) as (
        app,
        recorder,
        store,
        source,
        bodies,
    ):
        assert recorder.run(lambda: app.send("task")) == "done"
        sid = app.session.session_id
        contexts = request_contexts(store, sid)
        assert len(contexts) == len(bodies) == 4
        assert observations(bodies[0]) == observations(bodies[1]) == []
        assert [observations(b)[0]["reports"][0]["report"]["status"] for b in bodies[2:]] == [
            "failed",
            "passed",
        ]
        for context, body in zip(contexts, bodies, strict=True):
            selection = decode_selection(context["diagnostics"])
            assert all(m in body["messages"] for m in selection.messages())
            if selection.reports:
                assert body["messages"][-1] == selection.messages()[0]
                assert selection.reports[0].freshness == "current"
        assert outputs(bodies[2])[-1]["status"] == "applied"
        assert "diagnostics" not in outputs(bodies[2])[-1]
        assert all("修改后检查观察" not in str(m.raw) for m in app.history())
        original_events = store.load_events(sid)
    original_recording = source.read_bytes()
    target.write_text("external change after recording\n")

    def no_read(*_):
        pytest.fail("replay must not observe live diagnostic files")

    monkeypatch.setattr(DiagnosticFiles, "__call__", no_read)
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    report = read_report(next((tmp_path / "offline").rglob("*.jsonl")))
    assert [r["context"] for r in report["turns"][-1]["requests"]] == contexts
    assert source.read_bytes() == original_recording
    assert target.read_text() == "external change after recording\n"
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == original_events


def test_change_after_checker_keeps_old_facts_but_suppresses_stale_model_findings(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(tmp_path, batch_editor([target]), config=config) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):
        append = store.append_hook_event

        def save(sid, kind, payload):
            append(sid, kind, payload)
            if kind == "hook_finished" and "diagnostics" in payload["result"]:
                target.write_text("value = 99\n")

        monkeypatch.setattr(store, "append_hook_event", save)
        assert recorder.run(lambda: app.send("task")) == "done"
        model_report = observations(bodies[-1])[0]["reports"][0]
        assert model_report["freshness"] == "changed" and model_report["report"]["items"] == []
        stored = request_contexts(store, app.session.session_id)[-1]["diagnostics"]["reports"][0]
        assert stored["report"] == store.load_hooks(app.session.session_id)[-1]["diagnostics"]
        assert "invalid syntax" in stored["report"]["items"][0]["message"]
        assert stored["freshness"] == "changed"


@pytest.mark.parametrize("field", ["session_id", "turn_id", "hook_id", "report", "omitted_count"])
def test_request_rejects_wrong_diagnostic_source_before_model_or_log_write(
    tmp_path, monkeypatch, field
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(tmp_path, batch_editor([target]), config=config) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):
        start = store.start_request

        def tamper(sid, tid, model, *, context=None):
            value = context["diagnostics"]
            if value["reports"]:
                if field == "hook_id":
                    value["reports"][0]["hook_id"] = "another-hook"
                elif field == "report":
                    value["reports"][0]["report"]["checker"] = "another-checker"
                else:
                    value[field] = 1 if field == "omitted_count" else "another-source"
            return start(sid, tid, model, context=context)

        monkeypatch.setattr(store, "start_request", tamper)
        with pytest.raises(ValueError):
            recorder.run(lambda: app.send("task"))
        assert len(bodies) == 2
        sid = app.session.session_id
        assert len(request_contexts(store, sid)) == 2
        assert store.load_edits(sid)[0]["status"] == "applied"
        assert store.load_hooks(sid)[-1]["diagnostics"]["status"] == "failed"
        assert app.execution_report().status == "failed"


def test_preview_does_not_query_diagnostics_and_new_turn_does_not_inherit_old_report(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    bodies = []
    edit = edit_model(target, "value =", bodies)

    def respond(request):
        if len(bodies) < 3:
            return edit(request)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=answer("done"))

    with hooked_app(tmp_path, respond, structured_hooks(), approve=lambda _: True) as (app, store):
        assert app.send("task") == "done"
        before = store.load_events(app.session.session_id)

        def fail(*_):
            pytest.fail("preview queried turn diagnostics")

        with monkeypatch.context() as patch:
            patch.setattr(DiagnosticContextReader, "__call__", fail)
            preview = app.preview_context("hypothetical")
            assert preview.diagnostics is None and preview.estimate.diagnostics_tokens == 0
            assert store.load_events(app.session.session_id) == before
        assert app.send("ordinary new input") == "done"
        assert observations(bodies[-1]) == []
        assert request_contexts(store, app.session.session_id)[-1]["diagnostics"]["reports"] == []


def test_old_hook_recording_without_diagnostic_observer_keeps_old_input(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    # 真实录制旧装配行为，不删除新轨迹中的字段来伪造没有发生的文件观察。
    with monkeypatch.context() as patch:
        patch.setattr("adam.bootstrap.DiagnosticContextReader", lambda *args, **kwargs: None)
        with captured_app(tmp_path, batch_editor([target]), config=config) as (
            app,
            recorder,
            store,
            source,
            bodies,
        ):
            assert recorder.run(lambda: app.send("task")) == "done"
            assert not any(observations(b) for b in bodies)
            assert all(
                "diagnostics" not in c for c in request_contexts(store, app.session.session_id)
            )
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


def test_diagnostic_source_failure_stops_before_model_and_keeps_failed_input(tmp_path, monkeypatch):
    def fail(*_):
        raise StorageError("injected diagnostic read failure")

    monkeypatch.setattr(DiagnosticContextReader, "__call__", fail)
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(
        tmp_path, lambda *_: httpx.Response(200, json=answer("wrong")), config=config
    ) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):
        with pytest.raises(StorageError, match="injected"):
            recorder.run(lambda: app.send("task"))
        assert bodies == [] and request_contexts(store, app.session.session_id) == []
        assert app.execution_report().status == "failed"
        assert all(m.raw.get("content") != "task" for m in app.history())


def test_diagnostics_count_toward_actual_request_budget_before_http(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(tmp_path, batch_editor([target]), config=config) as (
        app,
        recorder,
        store,
        _,
        bodies,
    ):
        builder = app._conversation.context_builder
        build = builder.build

        def bounded(**kwargs):
            result = build(**kwargs)
            if result.diagnostics and result.diagnostics.reports:
                assert result.estimate.diagnostics_tokens > 0
                # 模拟额度仅够原请求：独立诊断必须进入总额，不能绕过原预算检查。
                return replace(
                    result,
                    limits=ContextConfig(
                        max_input_tokens=result.estimate.input_tokens
                        - result.estimate.diagnostics_tokens,
                        safety_margin_tokens=0,
                        auto_compact=False,
                    ),
                )
            return result

        monkeypatch.setattr(builder, "build", bounded)
        with pytest.raises(BudgetError):
            recorder.run(lambda: app.send("task"))
        assert len(bodies) == 2 and len(request_contexts(store, app.session.session_id)) == 2
        assert store.load_edits(app.session.session_id)[0]["status"] == "applied"

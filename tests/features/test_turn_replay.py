"""T9.2：从标准应用录制，再运行真实内部流程；离线阶段禁止网络和命令进程。"""

import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace

import httpx

from adam.bootstrap import build_application
from adam.config.settings import AgentConfig, ReflectionConfig
from adam.llm.client import LLMClient
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter
from adam.recording.runtime import CaptureRuntime, replay_turn
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_subagents import call
from tests.features.test_task_planning import step
from tests.features.test_tool_loop import answer, tool_response


@contextmanager
def captured_app(
    root, respond, *, config=None, child_handler=None, history=None, confirm_browser=None
):
    """保留真实主/子应用组装，远端仅替换 HTTP 传输，所有本地副作用实际发生。"""
    config = config or config_at(root)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return respond(body, len(bodies))

    @contextmanager
    def child_client(cancel):
        with httpx.Client(transport=httpx.MockTransport(child_handler)) as child_http:
            yield LLMClient(config.llm, child_http)

    path = root / "capture.jsonl"
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        sid = store.create_session("recorded", config.system_prompt)
        store.select_session(sid)
        for question, reply in history or [("old question", "old answer")]:
            store.append_turn(sid, question, reply)
        with RecordingWriter(path, capture_seed(config, store, sid, "task", root)) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            runtime = CaptureRuntime(recorder)
            app = build_application(
                config,
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                root,
                confirm_command=lambda _: True,
                confirm_edit=lambda _: True,
                confirm_browser=confirm_browser,
                decorate_tools=runtime.tools,
                observe_conversation=runtime.bind,
                child_client_factory=child_client if child_handler is not None else None,
            )
            app.resume(sid)
            try:
                yield app, recorder, store, path, bodies
            finally:
                app.close()


def forbid_external_actions(monkeypatch):
    """阶段切换后阻止网络和子进程，缺失适配器必须使测试失败。"""

    def fail(*args, **kwargs):
        raise AssertionError("offline replay attempted an external action")

    monkeypatch.setattr(httpx.Client, "send", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)


def test_replay_runs_plan_reminder_and_tool_loop_without_repeating_command(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("evidence=42", encoding="utf-8")
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    ]

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call("read", "read_file", path="note.txt"),
                    call("command", "run_command", argv=argv),
                ]
            )
        elif number == 2:
            value = answer("premature")
        elif number == 3:
            assert any(
                m["role"] == "user" and "计划收尾提醒" in (m.get("content") or "")
                for m in body["messages"]
            )
            value = tool_response(
                [call("done", "update_plan", steps=[step("completed", ["read", "command"])])]
            )
        else:
            value = answer("verified 42")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "verified 42"
        session_file = next(config_at(tmp_path).sessions_dir.glob("*/*/*/*.jsonl"))
        original = session_file.read_bytes()
        assert len(bodies) == 4
    trace = source.read_bytes()
    (tmp_path / "note.txt").write_text("changed after recording", encoding="utf-8")
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert result["reply"] == "verified 42"
    assert (tmp_path / "counter").read_text() == "1"
    assert source.read_bytes() == trace and session_file.read_bytes() == original
    replay_file = next((tmp_path / "offline").glob("*/*/*/*.jsonl"))
    records = [json.loads(line) for line in replay_file.read_bytes().splitlines()]
    assert sum(item["type"] == "request_started" for item in records) == 4
    assert sum(item["type"] == "plan_updated" for item in records) == 2


def test_failed_tool_triggers_real_reflection_and_preserves_identity_sources(tmp_path, monkeypatch):
    config = replace(config_at(tmp_path), reflection=ReflectionConfig())

    def respond(body, number):
        if number == 1:
            value = tool_response([call("missing", "read_file", path="absent.txt")])
        elif number == 2:
            assert "你负责基于反馈" in body["messages"][0]["content"]
            value = answer(json.dumps(SUGGESTION))
        else:
            value = answer("missing, not yet fixed")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "missing, not yet fixed"
        original_reflection = store.load_reflections(app.session.session_id)
        assert len(bodies) == 3 and len(original_reflection) == 1
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    with SessionStore(tmp_path / "offline") as replay:
        sid = replay.list_sessions()[0].session_id
        assert replay.load_reflections(sid) == original_reflection


def test_changed_output_budget_reports_first_request_field_without_external_actions(
    tmp_path, monkeypatch
):
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("done"))) as (
        app,
        recorder,
        _,
        source,
        _,
    ):
        recorder.run(lambda: app.send("task"))
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline", agent=AgentConfig(max_output_tokens=1234))
    assert result["status"] == "different", result
    assert result["difference"]["path"] == "$.input.max_tokens"
    assert result["difference"]["expected"] == 4096
    assert result["difference"]["actual"] == 1234


def test_child_results_and_shared_budget_are_replayed_without_starting_child(tmp_path, monkeypatch):
    def child(request):
        return httpx.Response(200, json=answer("child evidence"))

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="read-only task")]
            )
        elif number == 2:
            response = json.loads(
                next(m["content"] for m in body["messages"] if m["role"] == "tool")
            )
            value = tool_response([call("wait", "wait_agents", agent_ids=[response["agent_id"]])])
        else:
            value = answer("collected child evidence")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, child_handler=child) as (app, recorder, _, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "collected child evidence"
        assert len(bodies) == 3
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert result["reply"] == "collected child evidence"
    assert not (tmp_path / "offline" / "_subagents").exists()


def test_memory_and_changed_project_rules_use_recorded_sources(tmp_path, monkeypatch):
    from adam.application.memories import MemoryService
    from adam.contracts.memory import MemoryCommand
    from adam.rules.tracker import RuleTracker

    rules = tmp_path / "AGENTS.md"
    rules.write_text("Keep original constraint.", encoding="utf-8")
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("done"))) as (
        app,
        recorder,
        _,
        source,
        bodies,
    ):
        app.manage_memory(MemoryCommand("add", "task requires original memory"))
        recorder.run(lambda: app.send("task"))
        assert "original constraint" in json.dumps(bodies[0])
        assert "original memory" in json.dumps(bodies[0])
    rules.write_text("changed rule", encoding="utf-8")

    def fail(*args, **kwargs):
        raise AssertionError("replay read current external context")

    monkeypatch.setattr(MemoryService, "select", fail)
    monkeypatch.setattr(RuleTracker, "snapshot", fail)
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result


def test_record_entry_closes_app_and_replay_entry_never_loads_config(tmp_path, monkeypatch, capsys):
    from adam import recordings
    from adam.application.subagents.manager import SubagentManager

    closed = []
    original = SubagentManager.close

    def close(manager):
        closed.append(manager)
        return original(manager)

    monkeypatch.setattr(SubagentManager, "close", close)
    monkeypatch.setattr(
        recordings,
        "InterruptibleTransport",
        lambda *_: httpx.MockTransport(lambda _: httpx.Response(200, json=answer("entry done"))),
    )
    source = tmp_path / "entry.jsonl"
    assert recordings.record_turn(config_at(tmp_path), tmp_path, source, "task") == "entry done"
    assert len(closed) == 1
    assert "entry done" in capsys.readouterr().out
    forbid_external_actions(monkeypatch)

    def no_config(*args):
        raise AssertionError("replay loaded model configuration")

    monkeypatch.setattr(recordings, "load_config", no_config)
    destination = tmp_path / "offline"
    assert recordings.main(["replay", str(source), "--output", str(destination)]) == 0
    report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "matched" and report["reply"] == "entry done"
    assert recordings.main(["inspect", str(source)]) == 0
    before = (destination / "report.json").read_bytes()
    assert recordings.main(["replay", str(source), "--output", str(destination)]) == 2
    assert (destination / "report.json").read_bytes() == before


def test_module_entry_works_in_independent_process_with_only_recording(tmp_path):
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("module done"))) as (
        app,
        recorder,
        _,
        source,
        _,
    ):
        recorder.run(lambda: app.send("task"))
    destination = tmp_path / "offline"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "adam.recordings",
            "replay",
            str(source),
            "--output",
            str(destination),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["status"] == "matched"
    assert (
        json.loads((destination / "report.json").read_text(encoding="utf-8"))["reply"]
        == "module done"
    )


def test_shared_reservation_uses_changed_limit_and_reports_difference(tmp_path, monkeypatch):
    with captured_app(
        tmp_path,
        lambda *_: httpx.Response(200, json=answer("done")),
        child_handler=lambda _: httpx.Response(200, json=answer("unused child")),
    ) as (app, recorder, _, source, _):
        recorder.run(lambda: app.send("task"))
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline", agent=AgentConfig(max_session_tokens=1))
    assert result["status"] == "different", result
    # 预算先停止，随后进入子任务清理；最早可观察差异是省略请求、开始清理。
    assert result["difference"]["path"] == "$.channel"
    assert result["difference"]["expected"] == "context"
    assert result["difference"]["actual"] == "delegation"
    assert result["actual_error"]["code"] == "session_token_limit"


def test_explicit_skill_body_is_replayed_after_source_changes(tmp_path, monkeypatch):
    from tests.features.test_standard_skill_metadata import standard

    skill_file, _ = standard(tmp_path, body=b"ORIGINAL_SKILL_GUIDANCE")
    with captured_app(tmp_path, lambda *_: httpx.Response(200, json=answer("skill done"))) as (
        app,
        recorder,
        _,
        source,
        bodies,
    ):
        selected = next(item.identifier for item in app.list_skills() if item.overridden_by is None)
        recorder.run(lambda: app.send("task", skill_id=selected))
        assert "ORIGINAL_SKILL_GUIDANCE" in json.dumps(bodies[0])
    skill_file.write_text("changed after recording", encoding="utf-8")
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result


def test_automatic_compaction_rebuilds_summary_and_next_request(tmp_path, monkeypatch):
    from adam.config.settings import ContextConfig

    config = replace(
        config_at(tmp_path),
        context=ContextConfig(
            max_input_tokens=20000,
            safety_margin_tokens=0,
            compression_trigger_ratio=0.35,
            compression_target_ratio=0.2,
        ),
    )

    def respond(body, number):
        if number == 1:
            assert "tools" not in body
            value = answer("summary: retain ORION")
        else:
            assert "summary: retain ORION" in json.dumps(body)
            value = answer("summary used")
        return httpx.Response(200, json=value)

    history = [(f"old {index} ORION", "中" * 1000) for index in range(8)]
    with captured_app(tmp_path, respond, config=config, history=history) as (
        app,
        recorder,
        store,
        source,
        bodies,
    ):
        assert recorder.run(lambda: app.send("task")) == "summary used"
        original = store.load_summary(app.session.session_id)
        assert original is not None and len(bodies) == 2
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    with SessionStore(tmp_path / "offline") as replay:
        assert replay.load_summary(replay.list_sessions()[0].session_id) == original


def test_review_uses_recorded_child_evidence_without_rereading_targets(tmp_path, monkeypatch):
    from tests.features.test_independent_review import reviewer
    from tests.features.test_subagents import outputs

    target = tmp_path / "calc.py"
    target.write_text("value = 5\n", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call(
                        "ask",
                        "request_review",
                        goal="value is 5",
                        criteria="value = 5",
                        paths=["calc.py"],
                    )
                ]
            )
        elif number == 2:
            value = tool_response(
                [call("collect", "collect_review", review_id=outputs(body)[-1]["review_id"])]
            )
        else:
            assert outputs(body)[-1]["status"] == "completed"
            value = answer("review collected")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, child_handler=reviewer) as (
        app,
        recorder,
        store,
        source,
        bodies,
    ):
        assert recorder.run(lambda: app.send("task")) == "review collected"
        expected = store.load_reviews(app.session.session_id)
        assert len(bodies) == 3 and expected[0]["status"] == "completed"
    target.write_text("now invalid for old review", encoding="utf-8")
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    with SessionStore(tmp_path / "offline") as replay:
        assert replay.load_reviews(replay.list_sessions()[0].session_id) == expected

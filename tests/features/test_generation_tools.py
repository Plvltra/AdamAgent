"""普通模型工具调用经过真实来源、作业快照与媒体；后端fixture不代表生成质量。"""

import json
from contextlib import contextmanager

import httpx
import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.contracts.consumption import Quantities
from adam.generation.journal import GenerationJournal
from adam.llm.client import LLMClient
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from tests.features.test_generation_jobs import Backend
from tests.features.test_media_evidence import wav
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response

ARGS = {"kind": "audio", "job_id": "bell", "prompt": "A short bell", "seed": 7}


@contextmanager
def generation_app(root, backend, replies, *, sid=None, quota=None):
    """只模拟模型HTTP和生成边界，实际运行原应用、工具执行器与本地存储。"""
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        root / "sessions",
        "Generate only on request; keep unknown costs unknown.",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    bodies = []
    queue = iter(replies)

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(queue))

    quota = quota or UnitBudget(Quantities(None, None, 3, 10000, 10))
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            root,
            generation_backends={"audio": backend},
            generation_budget=quota,
        )
        if sid:
            app.resume(sid)
        try:
            yield app, store, bodies, quota
        finally:
            app.close()


def test_generate_query_duplicate_and_restart_preserve_original_reference(tmp_path):
    backend = Backend()
    replies = [
        tool_response([call("create", "generate_media", **ARGS)]),
        tool_response([call("query", "generation_result", kind="audio", job_id="bell")]),
        tool_response([call("again", "generate_media", **ARGS)]),
        answer("delivered"),
    ]
    with generation_app(tmp_path, backend, replies) as (app, store, bodies, quota):
        assert app.send("Generate a bell, query it and deliver its reference") == "delivered"
        result, queried, duplicate = outputs(bodies[-1])
        assert result["ok"] and queried["media"] == result["media"] == duplicate["media"]
        assert duplicate["duplicate"] and backend.calls == 1
        ref = result["media"]
        assert ref["source"]["tool_call_id"] == "create"
        media = SessionMedia(store, MediaStore(store.root / "_media"))
        assert media.read(ref["media_id"], expected_version=ref["version"]).data == wav()
        sid = app.session.session_id
        assert quota.usage().known.calls == 1
    with generation_app(
        tmp_path,
        backend,
        [tool_response([call("repeated", "generate_media", **ARGS)]), answer()],
        sid=sid,
    ) as (app, _, bodies, quota):
        app.send("Deliver the same job again")
        recovered = outputs(bodies[-1])[-1]
        assert recovered["duplicate"] and recovered["media"] == ref and backend.calls == 1
        assert quota.usage().known.calls == 1


def test_timeout_remains_unknown_and_query_does_not_retry(tmp_path):
    def timeout(spec, cancel):
        raise TimeoutError("injected generation response loss")

    backend = Backend(timeout)
    with generation_app(
        tmp_path,
        backend,
        [
            tool_response([call("lost", "generate_media", **ARGS)]),
            tool_response([call("read", "generation_result", kind="audio", job_id="bell")]),
            answer(),
        ],
    ) as (app, _, bodies, quota):
        app.send("Generate once")
        result = outputs(bodies[-1])[-1]
        assert result["state"] == "unknown" and result["media"] is None
        assert result["charge"]["cost"] is None and quota.usage().unknown_units
        assert backend.calls == 1


def test_initial_snapshot_failure_never_dispatches(tmp_path, monkeypatch):
    def fail(self, job):
        raise OSError("injected disk write failure before dispatch")

    monkeypatch.setattr(GenerationJournal, "save", fail)
    backend = Backend()
    with generation_app(
        tmp_path, backend, [tool_response([call("save", "generate_media", **ARGS)]), answer()]
    ) as (app, _, bodies, _):
        app.send("Generate once")
        result = outputs(bodies[-1])[-1]
        assert result["state"] == "failed" and result["usage"]["calls"] == 0
        assert backend.calls == 0


def test_changed_duplicate_is_rejected_without_new_side_effect(tmp_path):
    backend = Backend()
    with generation_app(
        tmp_path,
        backend,
        [
            tool_response([call("first", "generate_media", **ARGS)]),
            tool_response([call("changed", "generate_media", **{**ARGS, "prompt": "different"})]),
            answer(),
        ],
    ) as (app, _, bodies, _):
        app.send("Generate once")
        assert not outputs(bodies[-1])[-1]["ok"] and backend.calls == 1


def test_corrupt_snapshot_prevents_restart_dispatch(tmp_path):
    backend = Backend()
    with generation_app(
        tmp_path, backend, [tool_response([call("first", "generate_media", **ARGS)]), answer()]
    ) as (app, store, _, _):
        app.send("Generate")
        sid = app.session.session_id
        path = store.root / "_media" / "generation" / sid / "audio" / "bell.json"
    path.write_text('{"broken":true}', encoding="utf-8")
    with generation_app(
        tmp_path,
        backend,
        [tool_response([call("retry", "generate_media", **ARGS)]), answer()],
        sid=sid,
    ) as (app, _, _, _):
        # 坏快照不能被默认值修补后当作新作业；原异常也允许上层停止轮次。
        with pytest.raises((KeyError, ValueError)):
            app.send("Generate same job")
        assert backend.calls == 1


def test_recovered_running_job_is_unknown_and_not_dispatched(tmp_path):
    backend = Backend()
    with generation_app(
        tmp_path, backend, [tool_response([call("first", "generate_media", **ARGS)]), answer()]
    ) as (app, store, _, _):
        app.send("Generate")
        sid = app.session.session_id
        path = store.root / "_media" / "generation" / sid / "audio" / "bell.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot.update(state="running", media=None, usage={key: None for key in snapshot["usage"]})
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with generation_app(
        tmp_path,
        backend,
        [
            tool_response([call("query", "generation_result", kind="audio", job_id="bell")]),
            answer(),
        ],
        sid=sid,
    ) as (app, _, bodies, quota):
        app.send("Query only")
        result = outputs(bodies[-1])[-1]
        assert result["state"] == "unknown" and result["usage"]["calls"] is None
        assert backend.calls == 1 and "calls" in quota.usage().unknown_units
        assert json.loads(path.read_text())["state"] == "unknown"


def test_final_snapshot_failure_keeps_media_without_regeneration(tmp_path, monkeypatch):
    original = GenerationJournal.save

    def fail_final(self, job):
        # 故障在真实媒体发布之后、最终状态原子替换之前；unknown补记仍使用真实存储。
        if job.state == "succeeded":
            raise OSError("injected final snapshot failure")
        original(self, job)

    monkeypatch.setattr(GenerationJournal, "save", fail_final)
    backend = Backend()
    with generation_app(
        tmp_path,
        backend,
        [
            tool_response([call("create", "generate_media", **ARGS)]),
            tool_response([call("again", "generate_media", **ARGS)]),
            answer(),
        ],
    ) as (app, _, bodies, _):
        app.send("Generate once")
        result = outputs(bodies[-1])[-1]
        assert result["state"] == "unknown" and result["media"] is not None
        assert result["duplicate"] and backend.calls == 1

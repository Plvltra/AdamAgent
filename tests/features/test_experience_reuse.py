"""E4.5：经验沿用 E1 的选择、版本、范围与过期机制，保留实际请求证据。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.contracts.errors import MemoryStoreError
from adam.contracts.memory import MemoryCommand
from tests.features.test_experience_candidates import candidate_app
from tests.features.test_memory_context import config_at, memory_records
from tests.features.test_reflection_loop import reflection_config
from tests.features.test_subagents import application, call, outputs
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_loop import answer, tool_response


def test_accepted_experience_reused_in_new_session_and_updated_deleted_or_expired(tmp_path):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        assert not app.preview_context("配置路径").memory.records
        saved = app.decide_experience(candidate["reflection_id"], "accept")
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=answer())

    with plan_app(tmp_path, handler, config=reflection_config(tmp_path)) as (app, store):
        app.new("新任务")
        app.send("另一个配置路径需要怎么处理")
        records = memory_records(sent[-1])
        assert records == [saved.to_dict()]
        events = [
            e for e in store.load_events(app.session.session_id) if e["type"] == "request_started"
        ]
        assert events[-1]["payload"]["context"]["memory"]["records"] == records
        assert records[0]["experience"]["verification"] == "unverified"
        app.new("无关任务")
        app.send("火星的卫星名称")
        assert memory_records(sent[-1]) == []
        changed = app.manage_memory(
            MemoryCommand(
                "update",
                "配置路径基于文件目录；只适用于当前解析器",
                saved.memory_id,
                saved.revision,
            )
        )[0]
        assert changed.experience == saved.experience
        app.send("配置路径")
        assert memory_records(sent[-1]) == [changed.to_dict()]
        expired = app.manage_memory(
            MemoryCommand(
                "update",
                changed.content,
                changed.memory_id,
                changed.revision,
                expires_at="2020-01-01T00:00:00Z",
            )
        )[0]
        app.send("配置路径")
        assert not memory_records(sent[-1])
        deleted = app.manage_memory(
            MemoryCommand("delete", memory_id=expired.memory_id, revision=expired.revision)
        )[0]
        assert deleted.experience == saved.experience
        app.send("配置路径")
        assert not memory_records(sent[-1])


@pytest.mark.parametrize("enabled,budget", [(False, 1024), (True, 1)])
def test_disabled_or_too_small_memory_budget_omits_experience(tmp_path, enabled, budget):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        app.decide_experience(candidate["reflection_id"], "accept")
    cfg = reflection_config(tmp_path)
    cfg = replace(cfg, memory=replace(cfg.memory, enabled=enabled, max_input_tokens=budget))
    with plan_app(tmp_path, lambda _: pytest.fail("preview must not request"), config=cfg) as (
        app,
        store,
    ):
        preview = app.preview_context("配置路径")
        assert preview.memory.records == ()
        assert preview.memory.status == ("disabled" if not enabled else "budget")


def test_changed_memory_directory_does_not_recreate_already_accepted_candidate(tmp_path):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        app.decide_experience(candidate["reflection_id"], "accept")
        sid, cid = app.session.session_id, candidate["reflection_id"]
    empty = tmp_path / "other-memory"
    cfg = replace(reflection_config(tmp_path), memories_dir=empty)
    with plan_app(tmp_path, lambda _: pytest.fail("must not request"), config=cfg) as (app, store):
        app.resume(sid)
        with pytest.raises(MemoryStoreError, match="不自动重建"):
            app.decide_experience(cid, "accept")
        assert not (empty / "memories.json").exists()


def test_same_memory_library_does_not_cross_project_scope(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    with candidate_app(owner) as (app, store, candidate, bodies, notices):
        app.decide_experience(candidate["reflection_id"], "accept")
    other = tmp_path / "other"
    other.mkdir()
    cfg = config_at(other, memories_dir=owner / "memory")
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=answer())

    with plan_app(other, handler, config=cfg) as (app, store):
        app.send("配置路径")
        assert not memory_records(sent[0])


def test_child_inherits_selected_experience_but_cannot_register_feedback(tmp_path):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        saved = app.decide_experience(candidate["reflection_id"], "accept")
    child_bodies = []

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        assert memory_records(body) == [saved.to_dict()]
        names = {t["function"]["name"] for t in body["tools"]}
        assert "report_feedback" not in names
        return httpx.Response(200, json=answer("子任务已读经验，尚需实际核验"))

    def parent(request):
        body = json.loads(request.content)
        found = outputs(body)
        if not found:
            data = tool_response(
                [call("spawn", "spawn_agent", name="配置检查", task="配置路径建议")]
            )
        elif not any("agents" in item for item in found):
            agent_id = found[0]["agent_id"]
            data = tool_response([call("wait", "wait_agents", agent_ids=[agent_id])])
        else:
            data = answer("汇总")
        return httpx.Response(200, json=data)

    cfg = reflection_config(tmp_path)
    cfg = replace(cfg, agent=replace(cfg.agent, max_session_tokens=200000))
    with application(tmp_path, parent, child, config=cfg) as (app, store):
        app.send("请委派检查配置路径")
        assert child_bodies
        assert store.load_reflections(app.session.session_id) == []

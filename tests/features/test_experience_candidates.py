"""E4.4：人工处理候选、失败恢复与记忆幂等；HTTP 模拟，两个存储实际落盘。"""

import json
from contextlib import contextmanager

import httpx
import pytest

from adam.cli.memory_commands import handle_memory_command
from adam.contracts.errors import MemoryInputError, MemoryStoreError, StorageError
from adam.contracts.memory import MemoryCommand
from adam.memory.store import MemoryStore
from tests.features.test_reflection_loop import reflection_config
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_subagents import call
from tests.features.test_task_planning import plan_app
from tests.features.test_tool_loop import answer, tool_response


@contextmanager
def candidate_app(root):
    bodies, notices = [], []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        data = (
            tool_response([call("missing", "read_file", path="missing")])
            if len(bodies) == 1
            else answer(json.dumps(SUGGESTION) if len(bodies) == 2 else "请检查路径")
        )
        return httpx.Response(200, json=data)

    with plan_app(root, handler, config=reflection_config(root), notices=notices) as (app, store):
        app.send("检查配置相对路径")
        candidate = app.experience_candidates()[0]
        yield app, store, candidate, bodies, notices


def test_view_rewrite_accept_restart_and_deleted_memory_never_revives(tmp_path):
    lines = []
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        sid, cid = app.session.session_id, candidate["reflection_id"]
        assert app.manage_memory(MemoryCommand("list")) == []
        assert candidate["candidate_status"] == "pending"
        assert candidate["verification"] == "unverified"
        assert any("/memory candidates" in text for text in notices)
        handle_memory_command("candidates", app, lines.append)
        handle_memory_command("candidate " + cid, app, lines.append)
        assert any("真实来源资料" in text for text in lines)
        handle_memory_command(
            "accept " + cid + " -- 配置路径先确认基准，尚未验证", app, lines.append
        )
        saved = app.manage_memory(MemoryCommand("list"))[0]
        assert saved.content == "配置路径先确认基准，尚未验证"
        assert saved.experience["reflection_id"] == cid
        assert saved.experience["verification"] == "unverified"
        assert len(bodies) == 3
        again = app.decide_experience(cid, "accept", "不能静默覆盖已有记忆")
        assert again == saved
        deleted = app.manage_memory(
            MemoryCommand("delete", memory_id=saved.memory_id, revision=saved.revision)
        )[0]
        assert app.decide_experience(cid, "accept") == deleted
        assert app.manage_memory(MemoryCommand("list")) == []
    with plan_app(
        tmp_path, lambda _: pytest.fail("must not request"), config=reflection_config(tmp_path)
    ) as (app, store):
        app.resume(sid)
        found = app.experience_candidates()[0]
        assert found["candidate_status"] == "accepted" and found["memory"]["deleted"]
        assert len(store.experience_decisions(sid)) == 1
        app.new("another session")
        assert app.experience_candidates() == []


def test_discard_is_terminal_and_never_creates_memory(tmp_path):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        cid = candidate["reflection_id"]
        handle_memory_command("discard " + cid, app, lambda _: None)
        assert app.decide_experience(cid, "discard") is None
        with pytest.raises(MemoryInputError):
            app.decide_experience(cid, "accept")
        with pytest.raises(MemoryInputError):
            app.decide_experience("foreign", "accept")
        assert app.manage_memory(MemoryCommand("list")) == []
        assert app.experience_candidates()[0]["candidate_status"] == "discarded"


@pytest.mark.parametrize("boundary", ["memory", "session"])
def test_accept_save_failure_can_resume_without_duplicate(tmp_path, monkeypatch, boundary):
    with candidate_app(tmp_path) as (app, store, candidate, bodies, notices):
        sid, cid = app.session.session_id, candidate["reflection_id"]

        def fail(*args, **kwargs):
            raise (
                MemoryStoreError("injected memory failure")
                if boundary == "memory"
                else StorageError("injected session failure")
            )

        with monkeypatch.context() as patch:
            patch.setattr(
                MemoryStore if boundary == "memory" else store,
                "_append" if boundary == "memory" else "decide_experience",
                fail,
            )
            with pytest.raises((MemoryStoreError, StorageError)):
                app.decide_experience(cid, "accept")
        saved = app.manage_memory(MemoryCommand("list"))
        assert len(saved) == (0 if boundary == "memory" else 1)
        assert store.experience_decisions(sid) == {}
    with plan_app(
        tmp_path, lambda _: pytest.fail("must not request"), config=reflection_config(tmp_path)
    ) as (app, store):
        app.resume(sid)
        state = app.experience_candidates()[0]["candidate_status"]
        assert state == ("pending" if boundary == "memory" else "saved_pending_log")
        accepted = app.decide_experience(cid, "accept")
        assert len(app.manage_memory(MemoryCommand("list"))) == 1
        assert accepted.revision == 1
        assert app.decide_experience(cid, "accept") == accepted


@pytest.mark.parametrize(
    "text",
    ["accept", "candidates extra", "discard id -- text", "accept id -- ", "candidate id -- text"],
)
def test_invalid_candidate_command_never_writes(tmp_path, text):
    with plan_app(
        tmp_path, lambda _: pytest.fail("must not request"), config=reflection_config(tmp_path)
    ) as (app, store):
        with pytest.raises(MemoryInputError):
            handle_memory_command(text, app, lambda _: None)
        assert app.session is None
        assert app.manage_memory(MemoryCommand("list")) == []

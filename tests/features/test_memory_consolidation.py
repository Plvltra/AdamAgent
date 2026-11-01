"""B07.2：确认候选的原子保存、来源链、重复确认和可安全撤销的业务边界。"""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from adam.application.memories import MemoryService
from adam.cli.memory_commands import handle_memory_command
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemorySubject
from adam.memory.project import project_identity
from adam.memory.store import MemoryStore
from adam.recording.runtime import replay_turn
from tests.features.test_memory_context import memory_records
from tests.features.test_memory_management import memory_app as memory_app
from tests.features.test_tool_loop import answer
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def duplicates(service):
    first = service.manage(MemoryCommand("add", "Python 约定：运行测试"))[0]
    second = service.manage(MemoryCommand("add", first.content))[0]
    plan = service.organize()
    candidate = next(c for c in plan.candidates if c.kind == "merge")
    return first, second, plan, candidate


def test_accept_creates_one_overview_with_sources_and_undo_restores_original_validity(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    service = MemoryService(store, tmp_path)
    first, second, plan, candidate = duplicates(service)
    overview = service.accept_organization(plan.organization_id, candidate.candidate_id)
    assert overview.content == first.content and overview.derivation.verification == "unverified"
    assert {(r.memory_id, r.revision) for r in overview.derivation.sources} == {
        (first.memory_id, 1),
        (second.memory_id, 1),
    }
    selected = service.select("Python", 3000)
    assert selected.records == (overview,)
    for old in (first, second):
        versions = store.history(old.memory_id, old.project)
        assert versions[0] == old and len(versions) == 2
        assert versions[-1].expires_at == overview.valid_from
    before = store.path.read_bytes()
    assert service.accept_organization(plan.organization_id, candidate.candidate_id) == overview
    assert store.path.read_bytes() == before
    reopened = MemoryService(MemoryStore(store.root), tmp_path)
    removed = reopened.undo_organization(overview.memory_id, overview.revision)
    assert removed.deleted and removed.derivation == overview.derivation
    restored = reopened.select("Python", 3000).records
    assert {r.memory_id for r in restored} == {first.memory_id, second.memory_id}
    assert all(r.revision == 3 and r.expires_at is None for r in restored)
    assert len(reopened.store.history(overview.memory_id, overview.project)) == 2
    unchanged = reopened.store.path.read_bytes()
    assert reopened.accept_organization(plan.organization_id, candidate.candidate_id) == removed
    assert reopened.store.path.read_bytes() == unchanged


def test_conflict_requires_explicit_text_and_preserves_every_source(memory_app):
    subject = MemorySubject("owner", "维护者")
    first = memory_app.manage_memory(MemoryCommand("add", "Python 用方案甲", subject=subject))[0]
    second = memory_app.manage_memory(MemoryCommand("add", "Python 用方案乙", subject=subject))[0]
    plan = memory_app.organize_memory()
    candidate = plan.candidates[0]
    assert candidate.kind == "conflict"
    with pytest.raises(MemoryInputError, match="明确提供"):
        memory_app.accept_memory_organization(plan.organization_id, candidate.candidate_id)
    shown = []
    handle_memory_command(
        f"tidy accept {plan.organization_id} {candidate.candidate_id} -- Python 当前采用方案乙",
        memory_app,
        shown.append,
    )
    overview = memory_app.preview_context("Python").memory.records[0]
    assert overview.content == "Python 当前采用方案乙" and overview.subject == subject
    assert {r.memory_id for r in overview.derivation.sources} == {first.memory_id, second.memory_id}
    assert "尚未验证" in shown[0] and memory_app.session is None
    handle_memory_command(f"tidy undo {overview.memory_id} --revision 1", memory_app, shown.append)
    assert "已撤销" in shown[-1]


@pytest.mark.parametrize("operation", ["accept", "undo"])
def test_atomic_save_failure_never_partially_ends_or_restores_sources(
    tmp_path, monkeypatch, operation
):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    _, _, plan, candidate = duplicates(service)
    overview = None
    if operation == "undo":
        overview = service.accept_organization(plan.organization_id, candidate.candidate_id)
    before = service.store.path.read_bytes()

    def fail(*args):
        raise OSError("injected atomic memory replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(MemoryStoreError):
        if operation == "accept":
            service.accept_organization(plan.organization_id, candidate.candidate_id)
        else:
            service.undo_organization(overview.memory_id, 1)
    assert service.store.path.read_bytes() == before
    reopened = MemoryService(MemoryStore(service.store.root), tmp_path)
    assert len(reopened.select("Python", 3000).records) == (2 if operation == "accept" else 1)


@pytest.mark.parametrize("change", ["source", "delete", "result", "revision"])
def test_undo_refuses_to_overwrite_changes_after_consolidation(tmp_path, change):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    first, _, plan, candidate = duplicates(service)
    overview = service.accept_organization(plan.organization_id, candidate.candidate_id)
    if change == "source":
        service.manage(MemoryCommand("update", "后来编辑的内容", first.memory_id, 2))
    elif change == "delete":
        service.manage(MemoryCommand("delete", memory_id=first.memory_id, revision=2))
    elif change == "result":
        service.manage(MemoryCommand("update", "后来编辑的概览", overview.memory_id, 1))
    before = service.store.path.read_bytes()
    with pytest.raises(MemoryInputError):
        service.undo_organization(overview.memory_id, 2 if change in {"result", "revision"} else 1)
    assert service.store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["updated", "deleted", "expired", "scope"])
def test_stale_or_wrong_scope_confirmation_never_changes_current_memory(tmp_path, change):
    instant = datetime.now(UTC)
    store = MemoryStore(tmp_path / "memory", clock=lambda: instant)
    service = MemoryService(store, tmp_path)
    first, _, plan, candidate = duplicates(service)
    if change == "updated":
        service.manage(MemoryCommand("update", "新约定", first.memory_id, 1))
    elif change == "deleted":
        service.manage(MemoryCommand("delete", memory_id=first.memory_id, revision=1))
    elif change == "expired":
        service.manage(
            MemoryCommand(
                "update", first.content, first.memory_id, 1, expires_at=instant.isoformat()
            )
        )
    before = store.path.read_bytes()
    with pytest.raises((MemoryInputError, MemoryStoreError)):
        service.accept_organization(
            plan.organization_id, candidate.candidate_id, global_scope=change == "scope"
        )
    assert store.path.read_bytes() == before


def test_overview_never_extends_ttl_and_late_undo_does_not_revive_expired_fact(tmp_path):
    instant = datetime.now(UTC)
    clock = [instant]
    store = MemoryStore(tmp_path / "memory", clock=lambda: clock[0])
    service = MemoryService(store, tmp_path)
    end = (instant + timedelta(hours=1)).isoformat()
    for _ in range(2):
        service.manage(MemoryCommand("add", "Python 临时约定", expires_at=end))
    plan = service.organize()
    overview = service.accept_organization(plan.organization_id, plan.candidates[0].candidate_id)
    assert overview.expires_at == end
    clock[0] = instant + timedelta(hours=2)
    service.undo_organization(overview.memory_id, 1)
    current = store.list_records(project_identity(tmp_path))
    assert len(current) == 2 and all(not r.active(clock[0]) for r in current)


def test_overview_evidence_replays_without_loading_candidate_or_live_memory(tmp_path, monkeypatch):
    def respond(body, number):
        assert number == 1
        chosen = memory_records(body)
        assert len(chosen) == 1 and len(chosen[0]["derivation"]["sources"]) == 2
        return httpx.Response(200, json=answer("概览已提供"))

    with captured_app(tmp_path, respond) as (app, recorder, store, source, _):
        for _ in range(2):
            app.manage_memory(MemoryCommand("add", "task 用这条约定"))
        plan = app.organize_memory()
        result = app.accept_memory_organization(
            plan.organization_id, plan.candidates[0].candidate_id
        )
        assert recorder.run(lambda: app.send("task")) == "概览已提供"
        request = next(
            e for e in store.load_events(app.session.session_id) if e["type"] == "request_started"
        )
        assert request["payload"]["context"]["memory"]["records"] == [result.to_dict()]
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "replay")["status"] == "matched"


def test_forged_overview_reference_cannot_be_reopened(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    _, _, plan, candidate = duplicates(service)
    result = service.accept_organization(plan.organization_id, candidate.candidate_id)
    data = json.loads(service.store.path.read_bytes())
    data["records"][-1]["derivation"]["sources"][0]["revision"] = 999
    service.store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MemoryStoreError):
        service.store.history(result.memory_id, result.project)

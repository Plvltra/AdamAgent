"""B07.1：真实记忆快照生成离线候选，不调用模型，也不偷偷采用整理结果。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adam.application.memories import MemoryService
from adam.cli.memory_commands import handle_memory_command
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemorySubject
from adam.memory.organization import load_organization, propose
from adam.memory.store import MemoryStore
from tests.features.test_memory_management import memory_app as memory_app


def test_four_candidate_types_keep_original_bytes_and_exact_sources(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    service = MemoryService(store, tmp_path)
    first = service.manage(MemoryCommand("add", "改动后运行单元测试"))[0]
    second = service.manage(MemoryCommand("add", first.content))[0]
    owner = MemorySubject("deploy", "发布系统")
    rolling = service.manage(MemoryCommand("add", "使用滚动发布", subject=owner))[0]
    blue = service.manage(MemoryCommand("add", "使用蓝绿发布", subject=owner))[0]
    keep = service.manage(MemoryCommand("add", "周末去公园散步"))[0]
    expired = service.manage(
        MemoryCommand("add", "临时连接口令", expires_at="2020-01-01T00:00:00Z")
    )[0]
    future = service.manage(
        MemoryCommand("add", "未来年度约定", valid_from="2100-01-01T00:00:00Z")
    )[0]
    before = store.path.read_bytes()
    organized = service.organize()
    assert store.path.read_bytes() == before
    kinds = {}
    for candidate in organized.candidates:
        kinds.setdefault(candidate.kind, set()).update(r.memory_id for r in candidate.records)
    assert kinds == {
        "merge": {first.memory_id, second.memory_id},
        "conflict": {rolling.memory_id, blue.memory_id},
        "keep": {keep.memory_id, future.memory_id},
        "expired": {expired.memory_id},
    }
    reopened = MemoryService(MemoryStore(store.root), tmp_path)
    assert reopened.organize(organized.organization_id) == organized
    assert reopened.organize().candidates == organized.candidates
    assert len({r.memory_id for c in organized.candidates for r in c.records}) == 7


def test_project_global_and_same_name_people_are_separate_before_grouping(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    first = service.manage(
        MemoryCommand("add", "喜欢咖啡", subject=MemorySubject("wang-dev", "王伟"))
    )[0]
    second = service.manage(
        MemoryCommand("add", "喜欢咖啡", subject=MemorySubject("wang-art", "王伟"))
    )[0]
    common = service.manage(MemoryCommand("add", "喜欢咖啡", global_scope=True))[0]
    (tmp_path / "other").mkdir()
    other = MemoryService(service.store, tmp_path / "other")
    other.manage(MemoryCommand("add", "喜欢咖啡"))
    plan = service.organize()
    assert {c.kind for c in plan.candidates} == {"keep"}
    assert {c.records[0] for c in plan.candidates} == {first, second}
    assert service.organize(global_scope=True).candidates[0].records == (common,)
    with pytest.raises(MemoryStoreError):
        other.organize(plan.organization_id)
    with pytest.raises(MemoryStoreError):
        service.organize(plan.organization_id, global_scope=True)


def test_different_or_negated_claims_are_never_presented_as_safe_merge(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    service.manage(MemoryCommand("add", "production deployment must use rolling release"))
    service.manage(MemoryCommand("add", "production deployment must not use rolling release"))
    candidates = service.organize().candidates
    assert len(candidates) == 1 and candidates[0].kind == "conflict"
    assert "未判定谁正确" in candidates[0].reason
    assert len(candidates[0].records) == 2


def test_cli_creation_and_reopen_show_versions_without_starting_a_chat(memory_app):
    item = memory_app.manage_memory(MemoryCommand("add", "维护约定"))[0]
    shown = []
    handle_memory_command("tidy", memory_app, shown.append)
    assert memory_app.session is None
    assert "原记忆未修改" in shown[0] and f"{item.memory_id} v1" in "\n".join(shown)
    identity = shown[0].split()[1]
    reopened = []
    handle_memory_command(f"tidy {identity}", memory_app, reopened.append)
    assert reopened == shown and memory_app.session is None
    with pytest.raises(MemoryInputError):
        handle_memory_command("tidy ../outside", memory_app, shown.append)


def test_saved_candidate_remains_original_when_live_fact_is_changed(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    item = service.manage(MemoryCommand("add", "Python 使用旧约定"))[0]
    plan = service.organize()
    newer = service.manage(MemoryCommand("update", "Python 使用新约定", item.memory_id, 1))[0]
    assert service.organize(plan.organization_id).candidates[0].records == (item,)
    assert service.select("Python", 2000).records == (newer,)


@pytest.mark.parametrize("fault", ["kind", "version", "content", "duplicate", "truncated"])
def test_damaged_or_misclassified_proposal_is_not_loaded(tmp_path, fault):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    service.manage(MemoryCommand("add", "原约定"))
    service.manage(MemoryCommand("add", "原约定"))
    plan = service.organize()
    before = service.store.path.read_bytes()
    path = service.store.root / "organizations" / f"{plan.organization_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if fault == "kind":
        data["candidates"][0]["kind"] = "keep"
    elif fault == "version":
        data["version"] = 2
    elif fault == "content":
        data["candidates"][0]["records"][0]["content"] = "changed meaning"
    elif fault == "duplicate":
        data["candidates"].append(data["candidates"][0])
    raw = b"{unfinished" if fault == "truncated" else json.dumps(data).encode("utf-8")
    path.write_bytes(raw)
    with pytest.raises(MemoryStoreError):
        load_organization(service.store.root, plan.organization_id, plan.project)
    assert path.read_bytes() == raw and service.store.path.read_bytes() == before


def test_candidate_save_failure_reports_no_success_and_preserves_memory(memory_app, monkeypatch):
    item = memory_app.manage_memory(MemoryCommand("add", "必须保留的资料"))[0]
    original = Path.open

    def fail(path, *args, **kwargs):
        # 只模拟候选文件写入失败，原记忆仍通过真实文件系统读取。
        if path.parent.name == "organizations" and args and args[0] == "xb":
            raise PermissionError("injected proposal write failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    shown = []
    with pytest.raises(MemoryStoreError):
        handle_memory_command("tidy", memory_app, shown.append)
    assert shown == [] and memory_app.manage_memory(MemoryCommand("list")) == [item]


@pytest.mark.parametrize("fault", ["scope", "deleted", "duplicate", "limit", "clock"])
def test_invalid_source_snapshot_does_not_produce_partial_candidate(tmp_path, fault):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    item = service.manage(MemoryCommand("add", "source"))[0]
    records = [item]
    now = datetime.now(UTC)
    if fault == "scope":
        records = [replace(item, project=None)]
    elif fault == "deleted":
        records = [replace(item, deleted=True)]
    elif fault == "duplicate":
        records.append(item)
    elif fault == "limit":
        records = [item] * 257
    else:
        now = now.replace(tzinfo=None)
    with pytest.raises(MemoryInputError):
        propose(records, item.project, now)

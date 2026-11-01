"""B06.2：主体和事实时间经真实存储、CLI 与模型输入保持，替代使用一次原子提交。"""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from adam.application.memories import MemoryService
from adam.cli.memory_commands import handle_memory_command
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemoryReference, MemorySource, MemorySubject
from adam.memory.project import project_identity
from adam.memory.store import MemoryStore
from adam.recording.runtime import replay_turn
from tests.features.test_memory_context import memory_records
from tests.features.test_memory_management import memory_app as memory_app
from tests.features.test_semantic_memory import LocalVectors, service_at
from tests.features.test_tool_loop import answer
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def test_cli_subject_interval_and_source_are_visible(memory_app):
    app = memory_app
    shown = []
    handle_memory_command(
        "add --subject wang-dev --subject-name 王伟 --valid-from 2100-01-01T00:00:00Z -- 偏好单元测试",
        app,
        shown.append,
    )
    record = app.manage_memory(MemoryCommand("list"))[0]
    assert record.subject == MemorySubject("wang-dev", "王伟")
    assert record.valid_from == "2100-01-01T00:00:00Z"
    assert "尚未生效" in "\n".join(shown) and "@wang-dev" in "\n".join(shown)
    assert not app.preview_context("王伟偏好").memory.records
    handle_memory_command(
        f"update {record.memory_id} --revision 1 --valid-from 2020-01-01T00:00:00Z -- 偏好单元测试",
        app,
        shown.append,
    )
    changed = app.manage_memory(MemoryCommand("list"))[0]
    assert changed.subject == record.subject and changed.active(datetime.now(UTC))
    assert app.preview_context("@wang-dev 单元测试").memory.records == (changed,)


def test_same_name_requires_explicit_identity_before_embedding(tmp_path):
    backend = LocalVectors()
    service, _ = service_at(tmp_path, backend)
    first = service.manage(
        MemoryCommand("add", "先做单元测试", subject=MemorySubject("wang-dev", "王伟"))
    )[0]
    service.manage(
        MemoryCommand("add", "不偏好单元测试", subject=MemorySubject("wang-art", "王伟"))
    )
    service.manage(MemoryCommand("add", "默认偏好单元测试"))
    ambiguous = service.select("王伟怎样检验程序", 2000)
    assert ambiguous.records == () and "同名" in ambiguous.warning
    assert backend.passages_seen == backend.queries_seen == []
    chosen = service.select("@wang-dev 怎样检验程序", 2000)
    assert chosen.records == (first,) and not chosen.warning
    assert backend.passages_seen == [first.content]
    unknown = service.select("@unknown 怎样检验程序", 2000)
    assert unknown.records == () and unknown.warning
    assert service.select("@Wang-dev 怎样检验程序", 2000).warning
    assert backend.passages_seen == [first.content]


def test_replacement_ends_old_interval_atomically_and_keeps_exact_source(tmp_path):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    start = now + timedelta(days=1)
    root = tmp_path / "memory"
    store = MemoryStore(root, clock=lambda: now)
    subject = MemorySubject("writer", "张三")
    old = store.add("偏好茶", None, MemorySource("add"), subject=subject)
    new = store.add(
        "偏好咖啡",
        None,
        MemorySource("add"),
        valid_from=start.isoformat(),
        supersedes=MemoryReference(old.memory_id, old.revision),
    )
    assert new.subject == subject and new.supersedes == MemoryReference(old.memory_id, 1)
    ended = store.history(old.memory_id, None)[-1]
    assert ended.revision == 2 and ended.expires_at == new.valid_from
    assert ended.active(now) and not new.active(now)
    assert not ended.active(start) and new.active(start)
    reopened = MemoryStore(root, clock=lambda: start)
    assert reopened.history(old.memory_id, None) == (old, ended)
    assert reopened.history(new.memory_id, None) == (new,)
    reopened.delete(new.memory_id, None, MemorySource("delete"), expected_revision=1)
    assert not reopened.history(old.memory_id, None)[-1].active(start)


def test_replacement_failure_keeps_old_fact_and_does_not_save_new_one(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    old = store.add("旧偏好", None, MemorySource("add"))
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(MemoryStoreError):
        store.add("新偏好", None, MemorySource("add"), supersedes=MemoryReference(old.memory_id, 1))
    assert store.path.read_bytes() == before
    assert store.list_records(None) == [old] and old.active(datetime.now(UTC))


@pytest.mark.parametrize("fault", ["subject", "scope", "revision", "missing", "time"])
def test_invalid_replacement_cannot_end_another_fact(tmp_path, fault):
    store = MemoryStore(tmp_path / "memory")
    old = store.add(
        "旧偏好",
        None,
        MemorySource("add"),
        subject=MemorySubject("one", "王伟"),
        valid_from="2020-01-01T00:00:00Z",
    )
    project = str(tmp_path / "other") if fault == "scope" else None
    subject = MemorySubject("two", "王伟") if fault == "subject" else old.subject
    reference = MemoryReference(
        "00000000" if fault == "missing" else old.memory_id, 2 if fault == "revision" else 1
    )
    before = store.path.read_bytes()
    with pytest.raises(MemoryInputError):
        store.add(
            "新偏好",
            project,
            MemorySource("add"),
            subject=subject,
            valid_from="2010-01-01T00:00:00Z" if fault == "time" else None,
            supersedes=reference,
        )
    assert store.path.read_bytes() == before and store.history(old.memory_id, None) == (old,)


def test_current_selection_uses_new_preference_and_no_future_or_expired_facts(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    old = service.manage(MemoryCommand("add", "偏好茶", subject=MemorySubject("writer", "张三")))[0]
    new = service.manage(
        MemoryCommand("add", "偏好咖啡", supersedes=MemoryReference(old.memory_id, 1))
    )[0]
    service.manage(MemoryCommand("add", "偏好未来饮料", valid_from="2100-01-01T00:00:00Z"))
    service.manage(MemoryCommand("add", "偏好过期饮料", expires_at="2020-01-01T00:00:00Z"))
    assert service.select("@writer 偏好", 2000).records == (new,)
    assert service.store.history(old.memory_id, project_identity(tmp_path))[-1].revision == 2


@pytest.mark.parametrize(
    "command",
    [
        "add --subject one -- text",
        "add --subject-name 王伟 -- text",
        "add --supersedes 1234abcd -- text",
        "add --supersedes-revision 1 -- text",
        "add --valid-from 2020-01-01 -- text",
        "add --subject One --subject-name 王伟 -- text",
        "list --subject one --subject-name 王伟",
        "delete 1234abcd --revision 1 --valid-from 2020-01-01T00:00:00Z",
    ],
)
def test_bad_metadata_commands_do_not_write(memory_app, command):
    shown = []
    with pytest.raises(MemoryInputError):
        handle_memory_command(command, memory_app, shown.append)
    assert shown == [] and memory_app.manage_memory(MemoryCommand("list")) == []


def test_subject_and_replacement_evidence_replay_without_current_memory(tmp_path, monkeypatch):
    def respond(body, number):
        assert number == 1
        chosen = memory_records(body)
        assert len(chosen) == 1 and chosen[0]["content"] == "task 新偏好"
        assert chosen[0]["subject"]["key"] == "writer"
        assert chosen[0]["supersedes"]["revision"] == 1
        return httpx.Response(200, json=answer("已使用新事实"))

    with captured_app(tmp_path, respond) as (app, recorder, store, source, _):
        old = app.manage_memory(
            MemoryCommand("add", "task 旧偏好", subject=MemorySubject("writer", "张三"))
        )[0]
        new = app.manage_memory(
            MemoryCommand("add", "task 新偏好", supersedes=MemoryReference(old.memory_id, 1))
        )[0]
        assert recorder.run(lambda: app.send("task")) == "已使用新事实"
        request = next(
            e for e in store.load_events(app.session.session_id) if e["type"] == "request_started"
        )
        assert request["payload"]["context"]["memory"]["records"] == [new.to_dict()]
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "replay")["status"] == "matched"


def test_forged_cross_subject_reference_is_rejected_on_reopen(tmp_path):
    store = MemoryStore(tmp_path)
    old = store.add("旧偏好", None, MemorySource("add"), subject=MemorySubject("one", "王伟"))
    new = store.add(
        "新偏好", None, MemorySource("add"), supersedes=MemoryReference(old.memory_id, 1)
    )
    data = json.loads(store.path.read_bytes())
    data["records"][-1]["subject"]["key"] = "two"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MemoryStoreError):
        store.history(new.memory_id, None)

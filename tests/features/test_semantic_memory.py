"""B06.1：替代本地向量推理，实际经过范围过滤、版本存储、模型输入与记录重放。"""

import json
from dataclasses import replace

import httpx
import pytest

import adam.bootstrap as bootstrap
from adam.application.memories import MemoryService
from adam.config.settings import MemoryConfig
from adam.contracts.memory import MemoryCommand, MemorySource, validate_selection
from adam.documents.embedding import FastEmbedBackend
from adam.memory.project import project_identity
from adam.memory.semantic import SemanticMemoryRanker
from adam.memory.store import MemoryStore
from adam.recording.runtime import replay_turn
from tests.features.test_memory_context import config_at, memory_records
from tests.features.test_tool_loop import answer
from tests.features.test_turn_replay import captured_app, forbid_external_actions


class LocalVectors:
    """确定的向量夹具只验证流程；不用于证明真实 embedding 的同义召回效果。"""

    identity = "fixture:local:v1"

    def __init__(self):
        self.passages_seen = []
        self.queries_seen = []

    def passages(self, texts):
        self.passages_seen.extend(texts)
        return [[1.0, 0.0] if "单元测试" in text else [0.0, 1.0] for text in texts]

    def query(self, text):
        self.queries_seen.append(text)
        return [1.0, 0.0]


def service_at(root, backend, *, maximum=256, entries=8):
    store = MemoryStore(root / "memories")
    service = MemoryService(
        store,
        root,
        MemoryConfig(method="semantic", max_entries=entries),
        semantic=SemanticMemoryRanker(backend, max_records=maximum),
    )
    return service, store


def test_scope_and_expiry_filter_before_any_embedding_and_budget_preserves_full_versions(tmp_path):
    backend = LocalVectors()
    service, store = service_at(tmp_path, backend, entries=1)
    item = service.manage(MemoryCommand("add", "修改后运行单元测试"))[0]
    store.add("单元测试其他项目秘密", str(tmp_path / "other"), MemorySource("add"))
    store.add("单元测试过期", None, MemorySource("add"), expires_at="2020-01-01T00:00:00Z")
    deleted = store.add("单元测试已删除", None, MemorySource("add"))
    store.delete(deleted.memory_id, None, MemorySource("delete"), expected_revision=1)
    common = store.add("休息时间喝水", None, MemorySource("add"))
    selected = service.select("怎样检验刚改的程序？", 2000)
    assert selected.records == (item,) and selected.strategy == "semantic_v1"
    assert set(backend.passages_seen) == {item.content, common.content}
    assert selected.scores == ((item.memory_id, 1.0),)
    validate_selection(selected.to_dict())
    assert service.select("怎样检验刚改的程序？", 1).status == "budget"
    assert service.select("怎样检验刚改的程序？", 1).scores == ()
    assert set(backend.passages_seen) == {item.content, common.content}


def test_memory_and_model_versions_invalidate_cached_vectors(tmp_path):
    backend = LocalVectors()
    service, store = service_at(tmp_path, backend)
    item = service.manage(MemoryCommand("add", "先做单元测试"))[0]
    first = service.select("怎样检验？", 2000)
    assert first.records == (item,)
    service.select("怎样检验？", 2000)
    assert backend.passages_seen == [item.content]
    updated = service.manage(MemoryCommand("update", "先复核文档", item.memory_id, 1))[0]
    assert service.select("怎样检验？", 2000).records == ()
    assert backend.passages_seen == [item.content, updated.content]
    backend.identity = "fixture:local:v2"
    service.select("怎样检验？", 2000)
    assert backend.passages_seen[-2:] == [updated.content, updated.content]
    service.manage(MemoryCommand("delete", memory_id=item.memory_id, revision=2))
    count = len(backend.queries_seen)
    assert service.select("怎样检验？", 2000).records == ()
    assert len(backend.queries_seen) == count
    assert first.to_dict()["records"][0]["content"] == item.content
    assert store.history(item.memory_id, project_identity(tmp_path))[-1].deleted


@pytest.mark.parametrize("failure", ["raise", "zero", "nan", "dimension", "missing", "query"])
def test_backend_failure_uses_visible_deterministic_keyword_fallback(tmp_path, failure):
    backend = LocalVectors()
    original = backend.passages

    def passages(texts):
        if failure == "raise":
            raise RuntimeError("private backend details")
        if failure == "zero":
            return [[0, 0] for _ in texts]
        if failure == "nan":
            return [[float("nan"), 1] for _ in texts]
        if failure == "dimension":
            return [[1, 0, 0] for _ in texts]
        if failure == "missing":
            return []
        return original(texts)

    backend.passages = passages
    if failure == "query":
        backend.query = lambda _: [False, 1]
    service, _ = service_at(tmp_path, backend)
    item = service.manage(MemoryCommand("add", "Python 单元测试"))[0]
    selected = service.select("Python", 2000)
    assert selected.records == (item,)
    assert selected.strategy == "keyword_fallback_v1"
    assert selected.fallback_reason == "embedding_unavailable"
    assert selected.warning and not selected.scores and selected.embedding is None
    assert "private" not in json.dumps(selected.to_dict())
    validate_selection(selected.to_dict())


def test_over_limit_queries_and_candidate_sets_never_run_partial_semantic_search(tmp_path):
    backend = LocalVectors()
    service, _ = service_at(tmp_path, backend, maximum=1)
    service.manage(MemoryCommand("add", "Python 单元测试"))
    result = service.select("Python " * 200, 2000)
    assert result.fallback_reason == "query_limit"
    service.manage(MemoryCommand("add", "Python 代码规范"))
    result = service.select("Python", 2000)
    assert result.fallback_reason == "candidate_limit" and len(result.records) == 2
    assert backend.queries_seen == backend.passages_seen == []


def test_cancel_is_not_converted_to_keyword_result(tmp_path):
    backend = LocalVectors()

    def cancel(texts):
        raise KeyboardInterrupt()

    backend.passages = cancel
    service, _ = service_at(tmp_path, backend)
    service.manage(MemoryCommand("add", "Python 单元测试"))
    with pytest.raises(KeyboardInterrupt):
        service.select("Python", 2000)


def test_real_local_backend_missing_model_does_not_download(tmp_path, monkeypatch):
    calls = []

    def load(self):
        calls.append(self.allow_download)
        raise RuntimeError("injected local model absent")

    monkeypatch.setattr(FastEmbedBackend, "identity", property(lambda _: "fixture:missing-model"))
    monkeypatch.setattr(FastEmbedBackend, "_load", load)
    service, _ = service_at(tmp_path, FastEmbedBackend(tmp_path / "models", "local-only"))
    item = service.manage(MemoryCommand("add", "Python 单元测试"))[0]
    selected = service.select("Python", 2000)
    assert calls == [False] and selected.records == (item,)
    assert selected.strategy == "keyword_fallback_v1"
    assert not (tmp_path / "models").exists()


def test_semantic_snapshot_is_sent_recorded_and_replayed_without_embedding(tmp_path, monkeypatch):
    backend = LocalVectors()
    factory_calls = []

    def factory(cache, model, **kwargs):
        factory_calls.append((cache, model, kwargs))
        return backend

    monkeypatch.setattr(bootstrap, "FastEmbedBackend", factory)
    config = replace(config_at(tmp_path), memory=MemoryConfig(method="semantic"))

    def respond(body, number):
        assert number == 1
        assert memory_records(body)[0]["content"] == "修改后运行单元测试"
        return httpx.Response(200, json=answer("记忆已经提供"))

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, _):
        item = app.manage_memory(MemoryCommand("add", "修改后运行单元测试"))[0]
        # 复用录制夹具固定的 task 输入；与记忆正文仍无关键词重合。
        assert recorder.run(lambda: app.send("task")) == "记忆已经提供"
        request = next(
            e for e in store.load_events(app.session.session_id) if e["type"] == "request_started"
        )
        evidence = request["payload"]["context"]["memory"]
        assert evidence["strategy"] == "semantic_v1"
        assert evidence["records"] == [item.to_dict()]
        assert evidence["retrieval"]["embedding"] == backend.identity
    assert len(factory_calls) == 1
    assert factory_calls[0][0] == config.sessions_dir / "_documents" / "models"
    assert backend.queries_seen == ["task"]

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not load or compute embeddings")

    monkeypatch.setattr(bootstrap, "FastEmbedBackend", forbidden)
    backend.query = forbidden
    backend.passages = forbidden
    forbid_external_actions(monkeypatch)
    assert replay_turn(source, tmp_path / "replay")["status"] == "matched"

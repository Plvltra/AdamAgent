"""B11.1：真实文档的二层目录、逐字概览来源、版本和原子缓存边界。"""

from dataclasses import replace
from pathlib import Path

import pytest

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.documents.catalog import CatalogLimits, KnowledgeCatalogStore, build_catalog
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.snapshots import load_snapshot, save_snapshot
from adam.tool_calls.policy import PathPolicy


def catalog_index(root):
    docs = root / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "# Network\n\nA 服务断线最多重试两次。\n\nA 服务权限错误不得重试。\n", encoding="utf-8"
    )
    (docs / "b.md").write_text("# network\n\nB 服务失败需要用户手动重试。\n", encoding="utf-8")
    (docs / "c.md").write_text("# Storage\n\n写入完成之后才提交回答。\n", encoding="utf-8")
    policy = PathPolicy(root, PermissionConfig())
    return DocumentIndex(
        root / "cache", root, DocumentConfig(roots=(Path("docs"),)), policy.resolve
    )


def test_two_levels_merge_heading_topics_with_exact_cross_file_references(tmp_path):
    index = catalog_index(tmp_path)
    corpus = index.refresh()
    before = {p: p.read_bytes() for p in (tmp_path / "docs").iterdir()}
    cache_before = index.path.read_bytes()
    store = KnowledgeCatalogStore(tmp_path / "cache/catalog.json")
    catalog = store.prepare(corpus)
    assert len(catalog.topics) == 2 and sum(len(t.references) for t in catalog.topics) == len(
        corpus.chunks
    )
    topic = next(t for t in catalog.topics if t.title.casefold() == "network")
    assert len({r.path for r in topic.references}) == 2
    originals = {c.chunk_id: c for c in corpus.chunks}
    for piece in topic.overview:
        original = originals[piece.chunk_id]
        assert piece.text == original.text[piece.start : piece.end]
        assert not piece.text.startswith("#")
    assert len({originals[p.chunk_id].path for p in topic.overview}) == 2
    assert KnowledgeCatalogStore(store.path).prepare(index.refresh()) == catalog
    assert {p: p.read_bytes() for p in before} == before
    assert index.path.read_bytes() == cache_before


@pytest.mark.parametrize("limits", [CatalogLimits(max_topics=1), CatalogLimits(max_references=1)])
def test_overall_limit_rejects_incomplete_catalog(tmp_path, limits):
    index = catalog_index(tmp_path)
    path = tmp_path / "catalog.json"
    with pytest.raises(DocumentError, match="上限"):
        KnowledgeCatalogStore(path, limits=limits).prepare(index.refresh())
    assert not path.exists()


def test_overview_budget_keeps_all_references_and_marks_literal_truncation(tmp_path):
    corpus = catalog_index(tmp_path).refresh()
    catalog = build_catalog(corpus, CatalogLimits(max_overview_chars=7, max_overview_sources=2))
    assert all(sum(len(e.text) for e in t.overview) <= 7 for t in catalog.topics)
    assert any(e.truncated for t in catalog.topics for e in t.overview)
    assert sum(len(t.references) for t in catalog.topics) == len(corpus.chunks)
    assert all(len(t.overview) <= 2 for t in catalog.topics)


def test_source_update_delete_and_rule_change_rebuild_version_without_old_reference(tmp_path):
    index = catalog_index(tmp_path)
    store = KnowledgeCatalogStore(tmp_path / "catalog.json")
    old = store.prepare(index.refresh())
    (tmp_path / "docs/a.md").write_text("# Deployment\n\n发布前必须确认。", encoding="utf-8")
    (tmp_path / "docs/b.md").unlink()
    new = store.prepare(index.refresh())
    assert old.catalog_version != new.catalog_version
    assert not any(t.title.casefold() == "network" for t in new.topics)
    assert not any(r.path.endswith("b.md") for t in new.topics for r in t.references)
    smaller = KnowledgeCatalogStore(store.path, limits=CatalogLimits(max_overview_chars=3)).prepare(
        index.refresh()
    )
    assert (
        smaller.source_generation == new.source_generation
        and smaller.catalog_version != new.catalog_version
    )


@pytest.mark.parametrize("fault", ["missing_reference", "invalid_range", "version"])
def test_semantic_cache_corruption_is_visible_and_preserved(tmp_path, fault):
    index = catalog_index(tmp_path)
    store = KnowledgeCatalogStore(tmp_path / "catalog.json")
    corpus = index.refresh()
    store.prepare(corpus)
    payload = load_snapshot(store.path)
    if fault == "missing_reference":
        payload["topics"][0]["overview"][0]["chunk_id"] = "0" * 16
    elif fault == "invalid_range":
        payload["topics"][0]["overview"][0]["end"] = 9999
    else:
        payload["catalog_version"] = "incorrect"
    # 重新计算外层快照校验和，要求目录自己的关系校验也能拒绝损坏。
    save_snapshot(store.path, payload)
    before = store.path.read_bytes()
    with pytest.raises(DocumentError, match="缓存损坏"):
        store.prepare(corpus)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["save", "cancel"])
def test_failed_refresh_preserves_old_cache_without_returning_stale_overview(
    tmp_path, monkeypatch, fault
):
    index = catalog_index(tmp_path)
    token = CancellationToken()
    store = KnowledgeCatalogStore(tmp_path / "catalog.json", check_cancel=token.check)
    store.prepare(index.refresh())
    before = store.path.read_bytes()
    (tmp_path / "docs/c.md").write_text("# Storage\n\n新保存规则。", encoding="utf-8")
    corpus = index.refresh()
    if fault == "cancel":
        token.cancel()
    else:

        def deny(*args):
            raise PermissionError("injected atomic replacement failure")

        monkeypatch.setattr("adam.documents.snapshots.os.replace", deny)
    with pytest.raises((DocumentError, RequestInterrupted)):
        store.prepare(corpus)
    assert store.path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))


def test_mixed_file_versions_and_duplicate_chunks_are_not_a_valid_catalog(tmp_path):
    corpus = catalog_index(tmp_path).refresh()
    mixed = replace(corpus, chunks=(corpus.chunks[0], replace(corpus.chunks[1], version="0" * 64)))
    with pytest.raises(DocumentError, match="混合版本"):
        build_catalog(mixed)
    with pytest.raises(DocumentError, match="重复片段"):
        build_catalog(replace(corpus, chunks=(corpus.chunks[0], corpus.chunks[0])))


def test_empty_corpus_removes_previous_topics_and_untitled_file_uses_stem(tmp_path):
    index = catalog_index(tmp_path)
    path = tmp_path / "docs/plain.md"
    path.write_text("没有标题的文档正文", encoding="utf-8")
    corpus = index.refresh()
    store = KnowledgeCatalogStore(tmp_path / "catalog.json")
    catalog = store.prepare(corpus)
    assert any(t.title == "plain" for t in catalog.topics)
    empty = store.prepare(replace(corpus, chunks=(), file_count=0, generation="0" * 64))
    assert not empty.topics and not load_snapshot(store.path)["topics"]

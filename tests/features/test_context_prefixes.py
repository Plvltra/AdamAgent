"""B10.1：短说明与原文独立，真实索引/缓存验证版本与原子失败边界。"""

import json
from dataclasses import replace

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.documents.contextualization import ContextPrefixStore, ExtractiveContextBuilder
from adam.documents.models import DocumentError
from adam.documents.snapshots import load_snapshot, save_snapshot
from tests.features.test_retrieval_metrics import fixture_index


class RecordingBuilder(ExtractiveContextBuilder):
    def __init__(self):
        self.calls = []

    def build(self, request):
        self.calls.append(request)
        return super().build(request)


def test_notes_are_separate_from_exact_source_and_reused_after_restart(tmp_path):
    index = fixture_index(tmp_path)
    corpus = index.refresh()
    original = {p: p.read_bytes() for p in (tmp_path / "documents").iterdir()}
    snapshot = index.path.read_bytes()
    path = tmp_path / "cache" / "prefixes.json"
    builder = RecordingBuilder()
    notes = ContextPrefixStore(path).prepare(corpus, builder)
    assert len(notes) == len(corpus.chunks) == len(builder.calls)
    assert all(len(n.text) <= 300 and n.generator == builder.identity for n in notes)
    lookup = {c.chunk_id: c for c in corpus.chunks}
    assert all(n.source_version == lookup[n.chunk_id].version for n in notes)
    assert any(n.text != lookup[n.chunk_id].text for n in notes)
    assert all(lookup[b].path == n.path for n in notes for b in n.basis)
    assert ContextPrefixStore(path).prepare(index.refresh(), builder) == notes
    assert len(builder.calls) == len(notes)
    assert index.path.read_bytes() == snapshot
    assert {p: p.read_bytes() for p in original} == original
    assert all("text" in n and "input_sha256" in n for n in load_snapshot(path)["notes"])


def test_generation_receives_bounded_same_file_neighbors_and_explicit_truncation(tmp_path):
    index = fixture_index(tmp_path)
    corpus = index.refresh()
    builder = RecordingBuilder()
    ContextPrefixStore(tmp_path / "small.json", max_prefix_chars=40, max_input_chars=32).prepare(
        corpus, builder
    )
    assert all(
        len(r.target.text) + sum(len(n.text) for n in r.neighbors) <= 32 for r in builder.calls
    )
    assert any(r.target.original_chars > len(r.target.text) for r in builder.calls)
    assert all(
        len(r.neighbors) <= 2 and all(n.path == r.target.path for n in r.neighbors)
        for r in builder.calls
    )


def test_source_generator_and_window_changes_rebuild_without_reusing_old_notes(tmp_path):
    index = fixture_index(tmp_path)
    builder = RecordingBuilder()
    path = tmp_path / "cache" / "prefixes.json"
    store = ContextPrefixStore(path)
    first = store.prepare(index.refresh(), builder)
    affected = tmp_path / "documents" / "sessions.md"
    affected.write_text("# 改后的恢复\n\n新恢复规则。\n", encoding="utf-8")
    (tmp_path / "documents" / "network.md").unlink()
    second = store.prepare(index.refresh(), builder)
    assert not any(n.path.endswith("network.md") for n in second)
    assert not {n.chunk_id for n in first if n.path.endswith("sessions.md")} & {
        n.chunk_id for n in second
    }
    old_count = len(builder.calls)
    builder.identity = "changed-generator-v2"
    changed = store.prepare(index.refresh(), builder)
    assert len(builder.calls) == old_count + len(second)
    assert all(n.generator == "changed-generator-v2" for n in changed)
    shrunk = ContextPrefixStore(path, max_prefix_chars=20).prepare(index.refresh(), builder)
    assert all(len(n.text) <= 20 for n in shrunk)


@pytest.mark.parametrize("fault", ["oversized", "empty", "invalid", "raise", "cancel", "save"])
def test_failed_batch_preserves_previous_complete_cache(tmp_path, monkeypatch, fault):
    index = fixture_index(tmp_path)
    corpus = index.refresh()
    path = tmp_path / "notes.json"
    token = CancellationToken()
    store = ContextPrefixStore(path, check_cancel=token.check)
    old = store.prepare(corpus)
    before = path.read_bytes()
    builder = RecordingBuilder()
    builder.identity = "different"
    original = builder.build

    def fail_on_second(request):
        if len(builder.calls) == 1:
            if fault == "raise":
                raise ValueError("injected generator failure")
            if fault == "cancel":
                token.cancel()
            elif fault != "save":
                return {"oversized": "x" * 1001, "empty": " ", "invalid": None}[fault]
        return original(request)

    builder.build = fail_on_second
    if fault == "save":

        def deny_replace(*args):
            raise PermissionError("injected atomic replacement failure")

        monkeypatch.setattr("adam.documents.snapshots.os.replace", deny_replace)
    with pytest.raises((DocumentError, RequestInterrupted, ValueError)):
        store.prepare(corpus, builder)
    assert path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))
    assert ContextPrefixStore(path).prepare(corpus) == old


def test_corrupt_or_wrong_source_cache_is_not_silently_regenerated(tmp_path):
    index = fixture_index(tmp_path)
    corpus = index.refresh()
    path = tmp_path / "notes.json"
    store = ContextPrefixStore(path)
    store.prepare(corpus)
    data = load_snapshot(path)
    data["notes"][0]["path"] = "different source"
    save_snapshot(path, data)
    before = path.read_bytes()
    with pytest.raises(DocumentError, match="身份"):
        store.prepare(corpus)
    assert path.read_bytes() == before
    path.write_text(json.dumps({"version": "broken"}), encoding="utf-8")
    with pytest.raises(DocumentError, match="损坏"):
        store.prepare(corpus)


def test_empty_corpus_drops_old_notes_and_mixed_versions_are_rejected(tmp_path):
    index = fixture_index(tmp_path)
    corpus = index.refresh()
    store = ContextPrefixStore(tmp_path / "notes.json")
    store.prepare(corpus)
    mixed = replace(corpus, chunks=(corpus.chunks[0], replace(corpus.chunks[1], version="0" * 64)))
    with pytest.raises(DocumentError, match="多个版本"):
        store.prepare(mixed)
    assert store.prepare(replace(corpus, chunks=(), file_count=0)) == ()
    assert load_snapshot(store.path)["notes"] == []

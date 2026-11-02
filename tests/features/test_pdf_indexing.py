"""真实 PDF 页级检索、知识引用及失败边界；HTTP 模型回复不作为视觉能力证明。"""

import copy
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import ConfigError, RequestInterrupted, ToolError
from adam.documents.catalog_query import KnowledgeDirectory
from adam.documents.contextualization import ContextPrefixStore
from adam.documents.evaluation import evaluate
from adam.documents.evidence_audit import audit_summary, quote_chunk
from adam.documents.index import DocumentIndex
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import pack_results
from adam.tool_calls.policy import PathPolicy
from tests.features.support.pdf_data import make_pdf
from tests.features.test_document_tools import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def index_at(root, **limits):
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    return DocumentIndex(
        root / "cache",
        root,
        DocumentConfig(roots=(docs,), **limits),
        PathPolicy(root, PermissionConfig()).resolve,
    )


def summary_for(corpus, chunk):
    return {
        "source_generation": corpus.generation,
        "claims": [{"text": "retry is documented", "citations": [quote_chunk(chunk)]}],
    }


def test_identical_text_on_different_pages_has_distinct_identity_and_stable_cache(
    tmp_path, monkeypatch
):
    index = index_at(tmp_path)
    path = tmp_path / "docs/guide.pdf"
    data = make_pdf([{"text": "retry twice"}] * 22)
    path.write_bytes(data)
    (tmp_path / "docs/note.txt").write_text("plain text")
    from adam.documents.pdf import PdfReader

    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return PdfReader(*args, **kwargs)

    monkeypatch.setattr("adam.documents.pdf.PdfReader", counted)
    corpus = index.refresh()
    assert calls == [1]  # 全文索引只解析一次，不能为每页重新打开同一文件。
    chunks = [c for c in corpus.chunks if c.page is not None]
    assert [c.page for c in chunks] == list(range(1, 23))
    assert len({c.chunk_id for c in chunks}) == 22
    assert all(c.version == hashlib.sha256(data).hexdigest() for c in chunks)
    assert all(c.start_line == c.end_line == 1 for c in chunks)
    assert "page" not in next(c for c in corpus.chunks if c.page is None).to_dict()
    stamp = index.path.stat().st_mtime_ns
    assert index_at(tmp_path).refresh() == corpus
    assert index.path.stat().st_mtime_ns == stamp
    hits = DocumentSearch(index).search("retry", 3).hits
    assert [h.chunk.page for h in hits] == [1, 2, 3]


def test_search_tool_sends_page_evidence_records_and_replays_without_reopening_pdf(
    tmp_path, monkeypatch
):
    (tmp_path / "docs").mkdir()
    source = tmp_path / "docs/guide.pdf"
    data = make_pdf([{"text": "Cover"}, {"text": "retry twice", "image": True}])
    source.write_bytes(data)

    def respond(body, number):
        if number == 1:
            reply = tool_response([call("search", "search_documents", query="retry")])
        else:
            evidence = outputs(body)[-1]
            found = evidence["results"][0]
            assert evidence["ok"] and found["page"] == 2 and found["text"] == "retry twice"
            assert (
                found["path"] == str(source)
                and found["version"] == hashlib.sha256(data).hexdigest()
            )
            assert evidence["source_coverage"]["notices"][0]["kind"] == "unread_images"
            reply = answer("retry twice; physical page 2; image not inspected")
        return httpx.Response(200, json=reply)

    with captured_app(tmp_path, respond, config=config_at(tmp_path)) as (
        app,
        recorder,
        store,
        recording,
        _,
    ):
        result = recorder.run(lambda: app.send("task"))
        assert "physical page 2" in result
        events = json.dumps(store.load_events(app.session.session_id))
        assert hashlib.sha256(data).hexdigest() in events
        saved = next(m for m in store.load_messages(app.session.session_id) if m.role == "tool")
        evidence = json.loads(saved.content)
        assert evidence["results"][0]["page"] == 2
        assert evidence["results"][0]["version"] == hashlib.sha256(data).hexdigest()
    source.unlink()
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        "adam.documents.source.extract_pdf_all", lambda *a, **k: pytest.fail("replay parsed PDF")
    )
    assert replay_turn(recording, tmp_path / "replay")["status"] == "matched"


def test_catalog_context_and_frozen_candidate_preserve_physical_page(tmp_path):
    index = index_at(tmp_path)
    source = tmp_path / "docs/guide.pdf"
    source.write_bytes(make_pdf([{"text": "Cover"}, {"text": "retry twice"}, {"image": True}]))
    corpus = index.refresh()
    chunk = next(c for c in corpus.chunks if c.page == 2)
    directory = KnowledgeDirectory(index)
    overview = directory.overview()
    assert overview["source_coverage"]["notice_count"] == 1
    topic = overview["items"][0]
    fragments = directory.read_topic(topic["topic_id"], overview["catalog_version"])
    assert [c["page"] for c in fragments["items"]] == [1, 2]
    assert topic["overview"][0]["source"]["page"] == 1
    assert directory.overview() == overview
    requests = []

    class Builder:
        identity = "test-page-context"

        def build(self, request):
            requests.append(request)
            return "reference only"

    ContextPrefixStore(tmp_path / "contexts.json").prepare(corpus, Builder())
    assert [r.target.page for r in requests] == [1, 2]
    assert requests[0].neighbors[0].page == 2
    summary = summary_for(corpus, chunk)
    assert audit_summary(index, summary)["status"] == "provenance_valid"
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    proposal = {
        "statement": "retry twice",
        "applicability": "fixture",
        "reasoning": "quote only",
        "valid_from": None,
        "valid_until": None,
        "support": summary["claims"][0]["citations"],
        "counterevidence": [],
        "source_generation": corpus.generation,
    }
    candidate = store.create(proposal)
    assert store.read(candidate["candidate_id"])["proposal"]["support"][0]["page"] == 2
    changed = index_at(tmp_path, max_pdf_text_chars=900000)
    assert changed.path != index.path and changed.scope == index.scope
    assert (
        KnowledgeProposals(tmp_path / "knowledge", changed).read(candidate["candidate_id"])
        == candidate
    )


@pytest.mark.parametrize("claimed_page", [None, 1, True, 0])
def test_wrong_or_missing_page_never_passes_source_audit(tmp_path, claimed_page):
    index = index_at(tmp_path)
    (tmp_path / "docs/guide.pdf").write_bytes(make_pdf([{"text": "same"}, {"text": "same"}]))
    corpus = index.refresh()
    summary = summary_for(corpus, corpus.chunks[1])
    citation = summary["claims"][0]["citations"][0]
    if claimed_page is None:
        citation.pop("page")
    else:
        citation["page"] = claimed_page
    if type(claimed_page) is bool or claimed_page == 0:
        with pytest.raises(DocumentError, match="格式"):
            audit_summary(index, summary)
    else:
        result = audit_summary(index, summary)
        assert result["status"] == "invalid_evidence"
        assert result["claims"][0]["citations"][0]["status"] == "source_mismatch"


def test_same_size_same_mtime_edit_invalidates_quotes_catalog_and_deleted_source(tmp_path):
    index = index_at(tmp_path)
    source = tmp_path / "docs/guide.pdf"
    source.write_bytes(make_pdf([{"text": "retry twice"}]))
    corpus = index.refresh()
    summary = summary_for(corpus, corpus.chunks[0])
    directory = KnowledgeDirectory(index)
    overview = directory.overview()
    before = source.stat()
    # 使用等长替换，证明刷新不能只依赖 mtime/size。
    source.write_bytes(make_pdf([{"text": "retry three"}]))
    assert source.stat().st_size == before.st_size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert audit_summary(index, summary)["status"] == "stale_source"
    with pytest.raises(DocumentError, match="变化"):
        directory.read_topic(overview["items"][0]["topic_id"], overview["catalog_version"])
    current = DocumentSearch(index).search("three", 2)
    assert (
        current.hits[0].chunk.page == 1
        and current.hits[0].chunk.version != corpus.chunks[0].version
    )
    source.unlink()
    removed = index.refresh()
    assert removed.chunks == () and removed.notices == () and removed.file_count == 0


def test_scanned_mixed_and_unknown_pages_remain_visible_even_without_search_matches(tmp_path):
    index = index_at(tmp_path)
    source = tmp_path / "docs/scan.pdf"
    source.write_bytes(make_pdf([{"image": True}] * 7 + [{}]))
    result = pack_results(DocumentSearch(index).search("unknown", 2), 8000)
    assert result["status"] == "no_match" and result["results"] == []
    notice = result["source_coverage"]
    assert notice["status"] == "incomplete_pdf_perception"
    assert notice["notice_count"] == 8 and notice["notices_truncated"]
    assert len(notice["notices"]) == 5
    assert all(n["path"] == str(source) and n["kind"] == "image_only" for n in notice["notices"])
    assert index.refresh().notices[-1].kind == "no_extractable_text"
    with pytest.raises(DocumentError, match="输出限额"):
        pack_results(DocumentSearch(index).search("unknown", 2), 100)


@pytest.mark.parametrize("fault", ["encrypted", "corrupt", "text_limit", "page_limit"])
def test_failed_full_pdf_refresh_keeps_old_cache_without_returning_partial_text(tmp_path, fault):
    index = index_at(tmp_path, max_pdf_text_chars=30, max_pdf_pages=2)
    source = tmp_path / "docs/guide.pdf"
    source.write_bytes(make_pdf([{"text": "old"}]))
    index.refresh()
    previous = index.path.read_bytes()
    changed = {
        "encrypted": make_pdf([{"text": "PRIVATE"}], password="secret"),
        "corrupt": b"%PDF-private malformed body",
        "text_limit": make_pdf([{"text": "good"}, {"text": "X" * 31}]),
        "page_limit": make_pdf([{"text": "good"}] * 3),
    }[fault]
    source.write_bytes(changed)
    with pytest.raises(DocumentError) as error:
        DocumentSearch(index).search("good", 3)
    assert "PRIVATE" not in str(error.value) and "private malformed" not in str(error.value)
    assert index.path.read_bytes() == previous


@pytest.mark.parametrize("fault", ["modified", "revoked", "cancelled", "deadline"])
def test_parse_boundary_fault_does_not_publish_new_index(tmp_path, monkeypatch, fault):
    index = index_at(tmp_path)
    source = tmp_path / "docs/guide.pdf"
    source.write_bytes(make_pdf([{"text": "old"}]))
    index.refresh()
    previous = index.path.read_bytes()
    source.write_bytes(make_pdf([{"text": "new"}]))
    from adam.documents.source import extract_pdf_all

    revoked = False
    now = [0]
    authorize = index.source.authorize

    def checked(path):
        if revoked:
            raise ToolError("revoked")
        return authorize(path)

    def parsed(*args, **kwargs):
        nonlocal revoked
        result = extract_pdf_all(*args, **kwargs)
        if fault == "modified":
            source.write_bytes(make_pdf([{"text": "changed during parse"}]))
        elif fault == "revoked":
            revoked = True
        elif fault == "cancelled":
            raise RequestInterrupted()
        else:
            now[0] = 1000  # 只注入协作时钟边界，不声称验证了操作系统硬超时。
        return result

    index.source.authorize = checked
    monkeypatch.setattr("adam.documents.source.extract_pdf_all", parsed)
    monkeypatch.setattr("adam.documents.source.time.monotonic", lambda: now[0])
    with pytest.raises((DocumentError, ToolError, RequestInterrupted)):
        index.refresh()
    assert index.path.read_bytes() == previous


def test_pdf_outside_allowed_roots_is_not_opened_even_when_configured(tmp_path, monkeypatch):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    path = outside / "private.pdf"
    path.write_bytes(make_pdf([{"text": "PRIVATE"}]))
    policy = PathPolicy(root, PermissionConfig())
    index = DocumentIndex(root / "cache", root, DocumentConfig(roots=(outside,)), policy.resolve)
    original = Path.open

    def checked(source, *args, **kwargs):
        if source == path:
            pytest.fail("unapproved PDF opened")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked)
    with pytest.raises(ToolError, match="未授权"):
        index.refresh()
    assert not index.path.exists()


def test_cache_wrong_pdf_page_is_rejected_and_config_limits_load_from_toml(tmp_path):
    index = index_at(tmp_path)
    path = tmp_path / "docs/guide.pdf"
    path.write_bytes(make_pdf([{"text": "retry"}]))
    index.refresh()
    value = load_snapshot(index.path)
    value["documents"][str(path)]["chunks"][0]["page"] = None
    save_snapshot(index.path, value)
    with pytest.raises(DocumentError, match="损坏"):
        index.refresh()
    main = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    main.write_text('[documents]\nroots=["docs"]\nmax_pdf_pages=2\nmax_pdf_text_chars=50')
    config = load_config(main).documents
    assert config.max_pdf_pages == 2 and config.max_pdf_text_chars == 50
    for invalid in ("max_pdf_pages=true", "max_pdf_pages=2049", "max_pdf_text_chars=1000001"):
        main.write_text("[documents]\n" + invalid)
        with pytest.raises(ConfigError):
            load_config(main)


def test_unchanged_text_corpus_keeps_pre_pdf_generation_and_knowledge_scope(tmp_path):
    index = index_at(tmp_path)
    path = tmp_path / "docs/guide.md"
    path.write_text("retry twice", encoding="utf-8")
    corpus = index.refresh()
    legacy_scope = {
        "workspace": str(tmp_path.resolve()),
        "roots": [str((tmp_path / "docs").resolve())],
        "chunk_chars": index.config.chunk_chars,
    }
    assert corpus.generation == digest(
        {
            "scope": legacy_scope,
            "documents": {
                str(path): {
                    "version": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "chunks": [chunk.to_dict() for chunk in corpus.chunks],
                }
            },
        }
    )
    assert index.scope == legacy_scope
    assert KnowledgeProposals(tmp_path / "knowledge", index).root.name == digest(legacy_scope)[:24]


def test_evaluation_requires_physical_page_and_does_not_reward_same_text_on_other_page(tmp_path):
    index = index_at(tmp_path)
    (tmp_path / "docs/guide.pdf").write_bytes(make_pdf([{"text": "retry"}, {"text": "retry"}]))
    corpus = index.refresh()
    expected = corpus.chunks[1]
    cases = [
        {
            "id": "pdf",
            "kind": "exact",
            "query": "retry",
            "relevant": [
                {
                    "path": "guide.pdf",
                    "evidence": "retry",
                    "version": expected.version,
                    "start_line": 1,
                    "end_line": 1,
                    "page": 2,
                }
            ],
        }
    ]
    report = evaluate(DocumentSearch(index).search, cases, limit=1)
    assert report["hits"] == 0 and report["rows"][0]["retrieved"][0]["page"] == 1
    assert evaluate(DocumentSearch(index).search, cases, limit=2)["hits"] == 1
    missing = copy.deepcopy(cases)
    missing[0]["relevant"][0].pop("page")
    with pytest.raises(DocumentError, match="物理页码"):
        evaluate(lambda *args: pytest.fail("invalid PDF case reached search"), missing)

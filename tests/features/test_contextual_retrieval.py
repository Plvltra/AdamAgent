"""B10.2：语境增强评分与原文引用分离，真实文件/缓存/工具闭环。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import ConfigError
from adam.document_search import main
from adam.documents.contextualization import ContextPrefixStore
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from adam.documents.snapshots import load_snapshot
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import pack_results
from adam.tool_calls.policy import PathPolicy
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


class LabelVectors:
    """受控编码边界只识别代号，真实索引与向量缓存保持；不代表语义模型效果。"""

    identity = "label-vectors-v1"

    def __init__(self):
        self.inputs = []

    def passages(self, texts):
        self.inputs.extend(texts)
        return [[1.0, 0.0] if "ORION_NET" in t else [0.0, 1.0] for t in texts]

    def query(self, query):
        return [1.0, 0.0]


def corpus(root, method="keyword", enabled=True):
    docs = root / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "ORION_NET 是内部服务代号。\n\n它断线后最多重试两次。\n", encoding="utf-8"
    )
    config = DocumentConfig(roots=(Path("docs"),), method=method, contextualize=enabled)
    return DocumentIndex(root / "cache", root, config, PathPolicy(root, PermissionConfig()).resolve)


@pytest.mark.parametrize("method", ["keyword", "semantic", "hybrid"])
def test_prefix_can_recall_neighbor_but_citation_remains_original(tmp_path, method):
    index = corpus(tmp_path, method, enabled=False)
    backend = LabelVectors()
    before = DocumentSearch(index, backend).search("ORION_NET", 3)
    assert len(before.hits) == 1
    index.config = replace(index.config, contextualize=True)
    after = DocumentSearch(index, backend).search("ORION_NET", 3)
    assert len(after.hits) == 2 and after.contextualization.note_count == 2
    output = pack_results(after, 8000)
    target = next(item for item in output["results"] if "重试" in item["text"])
    assert target["text"] == "它断线后最多重试两次。" and "ORION_NET" not in target["text"]
    assert target["start_line"] == target["end_line"] == 3
    assert (
        "ORION_NET" in target["context_prefix"]["text"]
        and "不是引用原文" in output["context_notice"]
    )
    assert target["context_prefix"]["source_version"] == target["version"]
    assert after.generation == before.generation


def test_changed_prefix_reencodes_vectors_with_unchanged_original_chunk(tmp_path):
    index = corpus(tmp_path, "semantic")
    backend = LabelVectors()

    class Builder:
        identity = "v1"
        text = "ORION_NET"

        def build(self, request):
            return self.text

    builder = Builder()
    search = DocumentSearch(index, backend, context_builder=builder)
    first = search.search("ORION_NET", 3)
    assert len(first.hits) == 2 and len(backend.inputs) == 2
    search.search("ORION_NET", 3)
    assert len(backend.inputs) == 2
    builder.identity, builder.text = "v2", "OTHER_LABEL"
    second = search.search("ORION_NET", 3)
    assert len(backend.inputs) == 4 and len(second.hits) == 1
    assert first.generation == second.generation
    assert first.contextualization.notes_version != second.contextualization.notes_version
    assert first.hits[0].chunk.chunk_id in {c.chunk_id for c in index.refresh().chunks}


def test_source_update_delete_and_reopen_remove_old_context(tmp_path):
    index = corpus(tmp_path)
    first = DocumentSearch(index).search("ORION_NET", 3)
    path = tmp_path / "docs/guide.md"
    path.write_text("OTHER_LABEL 是更新后的代号。\n\n它最多重试一次。\n", encoding="utf-8")
    reopened = DocumentSearch(index)
    assert not reopened.search("ORION_NET", 3).hits
    updated = reopened.search("OTHER_LABEL", 3)
    assert updated.contextualization.notes_version != first.contextualization.notes_version
    assert all(
        n.source_version == h.chunk.version
        for n, h in zip(updated.context_prefixes, updated.hits, strict=True)
    )
    path.unlink()
    empty = reopened.search("OTHER_LABEL", 3)
    assert not empty.hits and empty.contextualization.note_count == 0
    assert load_snapshot(index.path.with_suffix(".contexts.json"))["notes"] == []


def test_disabled_context_never_reads_or_generates_prefixes(tmp_path, monkeypatch):
    index = corpus(tmp_path, enabled=False)
    monkeypatch.setattr(
        ContextPrefixStore, "prepare", lambda *_: pytest.fail("disabled context invoked")
    )
    result = DocumentSearch(index).search("ORION_NET", 3)
    assert result.contextualization is None and not result.context_prefixes
    assert not index.path.with_suffix(".contexts.json").exists()
    assert "contextualization" not in pack_results(result, 8000)


def test_invalid_cached_notes_fail_instead_of_falling_back_to_ordinary_search(tmp_path):
    index = corpus(tmp_path)
    search = DocumentSearch(index)
    search.search("ORION_NET", 3)
    path = index.path.with_suffix(".contexts.json")
    path.write_bytes(b"{half")
    with pytest.raises(DocumentError, match="损坏"):
        search.search("ORION_NET", 3)
    assert path.read_bytes() == b"{half"


def test_prefix_and_original_share_output_budget_without_merging_text(tmp_path):
    found = DocumentSearch(corpus(tmp_path)).search("ORION_NET", 3)
    packed = pack_results(found, 800)
    assert len(json.dumps(packed, ensure_ascii=False)) <= 800
    assert packed["status"] == "output_limit" and packed["contextualization"]
    assert not packed["results"]
    note = replace(found.context_prefixes[0], source_version="0" * 64)
    with pytest.raises(DocumentError, match="原文版本"):
        pack_results(replace(found, context_prefixes=(note,)), 8000)


def test_tool_logs_keep_original_and_prefix_separate_and_replay_does_not_rebuild(
    tmp_path, monkeypatch
):
    corpus(tmp_path)
    config = replace(
        config_at(tmp_path), documents=DocumentConfig(roots=(Path("docs"),), contextualize=True)
    )

    def respond(body, number):
        if number == 1:
            reply = tool_response([call("search", "search_documents", query="ORION_NET")])
        else:
            result = outputs(body)[-1]
            hit = next(r for r in result["results"] if "重试" in r["text"])
            assert hit["text"] == "它断线后最多重试两次。"
            assert hit["context_prefix"]["generator"] == "extractive-context-v1"
            reply = answer("根据 guide.md:3，最多两次。")
        return httpx.Response(200, json=reply)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, artifact, _):
        assert "两次" in recorder.run(lambda: app.send("task"))
        assert any(
            "context_prefix" in json.dumps(e)
            for e in store.load_events(app.session.session_id)
            if e["type"] == "tool_finished"
        )
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        ContextPrefixStore, "prepare", lambda *_: pytest.fail("replay regenerated prefixes")
    )
    assert replay_turn(artifact, tmp_path / "replay")["status"] == "matched"


@pytest.mark.parametrize(
    "setting", ["contextualize=1", "context_prefix_chars=0", "context_input_chars=8001"]
)
def test_bad_context_config_is_rejected_before_search(tmp_path, setting):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text("[documents]\n" + setting, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_and_valid_toml_expose_optional_context_without_llm(tmp_path, capsys):
    corpus(tmp_path)
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "cli-cache"),
                "--contextual",
                "search",
                "ORION_NET",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["contextualization"]["generator"] == "extractive-context-v1"
    assert len(result["results"]) == 2
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        "[documents]\ncontextualize=true\ncontext_prefix_chars=120\ncontext_input_chars=900",
        encoding="utf-8",
    )
    config = load_config(path).documents
    assert (
        config.contextualize
        and config.context_prefix_chars == 120
        and config.context_input_chars == 900
    )

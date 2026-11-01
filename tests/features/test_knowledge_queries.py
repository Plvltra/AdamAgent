"""B11.2：概览到原文、真实源变化、输出边界及模型工具录制回放。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.settings import DocumentConfig, PermissionConfig, ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.document_search import main
from adam.documents.catalog_query import KnowledgeDirectory, encode_catalog
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.recording.runtime import replay_turn
from adam.tool_calls.document_tools import register_documents
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from tests.features.test_knowledge_catalog import catalog_index
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def network_page(directory):
    result = directory.overview("network")
    assert result["total"] == 1
    return result, result["items"][0]


def test_overview_then_paged_originals_keep_cross_file_references_complete(tmp_path):
    index = catalog_index(tmp_path)
    directory = KnowledgeDirectory(index)
    overview, topic = network_page(directory)
    assert overview["coverage"] == "representative_excerpts_only"
    assert topic["file_count"] == 2
    offset, returned = 0, []
    while True:
        page = directory.read_topic(
            topic["topic_id"], overview["catalog_version"], offset=offset, limit=1
        )
        returned.extend(page["items"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    expected = [c.to_dict() for c in index.refresh().chunks if c.title.casefold() == "network"]
    assert returned == expected and len(returned) == topic["reference_count"]
    for item in returned:
        assert item["text"] in Path(item["path"]).read_text(encoding="utf-8")
    first = directory.overview(limit=1)
    rest = directory.overview(offset=1, limit=1, catalog_version=first["catalog_version"])
    assert first["items"][0]["topic_id"] != rest["items"][0]["topic_id"]
    assert not rest["has_more"]


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_changed_sources_reject_old_pages_then_rebuild_for_explicit_query(tmp_path, change):
    index = catalog_index(tmp_path)
    directory = KnowledgeDirectory(index)
    overview, topic = network_page(directory)
    source = tmp_path / "docs/b.md"
    if change == "edit":
        source.write_text("# network\n\nB 服务自动重试三次。", encoding="utf-8")
    else:
        source.unlink()
    with pytest.raises(DocumentError, match="旧引用失效"):
        directory.read_topic(topic["topic_id"], overview["catalog_version"])
    with pytest.raises(DocumentError, match="旧引用失效"):
        directory.overview(offset=1, catalog_version=overview["catalog_version"])
    current, item = network_page(KnowledgeDirectory(index))
    assert current["catalog_version"] != overview["catalog_version"]
    detail = directory.read_topic(item["topic_id"], current["catalog_version"], limit=20)
    assert all(c["text"] != "B 服务失败需要用户手动重试。" for c in detail["items"])


@pytest.mark.parametrize("failure", ["denied", "invalid_utf8", "cancelled"])
def test_unavailable_sources_do_not_fall_back_to_cached_overview(tmp_path, failure):
    index = catalog_index(tmp_path)
    directory = KnowledgeDirectory(index)
    overview, topic = network_page(directory)
    before = directory.store.path.read_bytes()
    if failure == "denied":

        def deny(path):
            raise ToolError("injected read permission withdrawal")

        index.source.authorize = deny
    elif failure == "invalid_utf8":
        (tmp_path / "docs/a.md").write_bytes(b"\xff")
    else:
        token = CancellationToken()
        token.cancel()
        index.check_cancel = token.check
    with pytest.raises((ToolError, DocumentError, RequestInterrupted)):
        directory.read_topic(topic["topic_id"], overview["catalog_version"])
    assert directory.store.path.read_bytes() == before


def test_removing_allowed_root_invalidates_old_reference_even_when_files_remain(tmp_path):
    index = catalog_index(tmp_path)
    overview, topic = network_page(KnowledgeDirectory(index))
    empty = tmp_path / "empty"
    empty.mkdir()
    policy = PathPolicy(tmp_path, PermissionConfig())
    narrowed = DocumentIndex(
        tmp_path / "cache", tmp_path, DocumentConfig(roots=(Path("empty"),)), policy.resolve
    )
    with pytest.raises(DocumentError, match="旧引用失效"):
        KnowledgeDirectory(narrowed).read_topic(topic["topic_id"], overview["catalog_version"])
    assert KnowledgeDirectory(narrowed).overview()["status"] == "no_match"


def test_output_limit_does_not_skip_unreturned_topic_or_split_original(tmp_path):
    index = catalog_index(tmp_path)
    directory = KnowledgeDirectory(index, max_chars=800)
    page = directory.overview()
    assert len(encode_catalog(page)) <= 800
    assert page["status"] == "output_limit" and page["next_offset"] == 0 and not page["items"]
    overview, topic = network_page(KnowledgeDirectory(index))
    detail = directory.read_topic(topic["topic_id"], overview["catalog_version"])
    assert len(encode_catalog(detail)) <= 800
    expected = KnowledgeDirectory(index).read_topic(topic["topic_id"], overview["catalog_version"])
    count = len(detail["items"])
    assert detail["items"] == expected["items"][:count]
    assert detail["next_offset"] == count and detail["has_more"]
    with pytest.raises(DocumentError, match="元数据"):
        KnowledgeDirectory(index, max_chars=512).overview("x" * 600)


@pytest.mark.parametrize(
    "args", [{"offset": 1}, {"limit": True}, {"query": "x" * 1001}, {"catalog_version": "bad"}]
)
def test_invalid_query_stops_before_creating_cache(tmp_path, args):
    index = catalog_index(tmp_path)
    with pytest.raises(DocumentError):
        KnowledgeDirectory(index).overview(**args)
    assert not index.path.parent.exists()


def test_unknown_topic_and_out_of_range_page_are_errors_not_evidence(tmp_path):
    directory = KnowledgeDirectory(catalog_index(tmp_path))
    page, topic = network_page(directory)
    with pytest.raises(DocumentError, match="没有这个主题"):
        directory.read_topic("0" * 16, page["catalog_version"])
    with pytest.raises(DocumentError, match="超出"):
        directory.read_topic(topic["topic_id"], page["catalog_version"], offset=999)
    assert directory.overview("不存在的ZXY主题")["status"] == "no_match"


def test_both_cli_entries_use_same_version_without_chat_config(tmp_path, capsys):
    catalog_index(tmp_path)
    options = [
        "--workspace",
        str(tmp_path),
        "--cache-dir",
        str(tmp_path / "cli-cache"),
        "--limit",
        "20",
    ]
    assert main([*options, "catalog", "network"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert (
        main(
            [*options, "read-topic", overview["items"][0]["topic_id"], overview["catalog_version"]]
        )
        == 0
    )
    detail = json.loads(capsys.readouterr().out)
    assert detail["kind"] == "original_fragments"
    assert {Path(c["path"]).name for c in detail["items"]} == {"a.md", "b.md"}
    assert main([*options, "read-topic", "0" * 16, overview["catalog_version"]]) == 1


def test_registered_read_only_tools_are_lazy_and_do_not_offer_arbitrary_paths(tmp_path):
    config = DocumentConfig(roots=(Path("missing"),))
    registry = register_documents(
        create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=8000)),
        tmp_path,
        config,
        tmp_path / "cache",
        PermissionConfig(),
    ).with_mode(lambda: ExecutionMode.PLAN)
    assert not (tmp_path / "cache").exists()
    for name in ("knowledge_overview", "read_knowledge_topic"):
        assert registry.capability(name) is ToolCapability.READ_ONLY
        assert registry.can_parallelize(name) is False
    bad = json.loads(registry.execute("knowledge_overview", '{"path":"C:/secret"}'))
    assert not bad["ok"] and "未知字段" in bad["error"]
    missing = json.loads(
        registry.execute("read_knowledge_topic", '{"topic_id":"0000000000000000"}')
    )
    assert not missing["ok"] and "必填" in missing["error"]
    assert not (tmp_path / "cache").exists()
    failed = json.loads(registry.execute("knowledge_overview", "{}"))
    assert not failed["ok"] and "路径不存在" in failed["error"]


def test_model_overview_to_original_keeps_logs_and_offline_replay_frozen(tmp_path, monkeypatch):
    catalog_index(tmp_path)
    config = replace(config_at(tmp_path), documents=DocumentConfig(roots=(Path("docs"),)))

    def respond(body, number):
        if number == 1:
            result = tool_response([call("overview", "knowledge_overview", query="network")])
        elif number == 2:
            overview = outputs(body)[-1]
            result = tool_response(
                [
                    call(
                        "details",
                        "read_knowledge_topic",
                        topic_id=overview["items"][0]["topic_id"],
                        catalog_version=overview["catalog_version"],
                        limit=20,
                    )
                ]
            )
        else:
            detail = outputs(body)[-1]
            assert any("手动重试" in item["text"] for item in detail["items"])
            assert len({item["path"] for item in detail["items"]}) == 2
            result = answer("A 与 B 重试规则不同，依据 a.md:3 与 b.md:3。")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, artifact, _):
        assert "规则不同" in recorder.run(lambda: app.send("task"))
        events = store.load_events(app.session.session_id)
        assert sum(e["type"] == "tool_finished" for e in events) == 2
        assert "catalog_version" in json.dumps(events)
    (tmp_path / "docs/a.md").write_text("完全变更的来源", encoding="utf-8")
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(DocumentIndex, "refresh", lambda *_: pytest.fail("replay re-read catalog"))
    assert replay_turn(artifact, tmp_path / "replay")["status"] == "matched"

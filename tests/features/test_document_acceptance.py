"""E2.5：真实子任务、权限撤销、取消与缓存故障的整体验收。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.search import DocumentSearch
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.policy import PathPolicy
from tests.features.test_document_embeddings import FakeEmbedding
from tests.features.test_document_index import make_index
from tests.features.test_document_tools import call_search, config_at, model_response
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_real_child_retrieves_documents_and_parent_collects_evidence(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "guide.md"
    source.write_text("配置要求使用绝对导入。", encoding="utf-8")
    parent_requests, child_requests = [], []

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        if len(parent_requests) == 1:
            data = tool_response([call("spawn", "spawn_agent", name="reader", task="查配置文档")])
        elif len(parent_requests) == 2:
            data = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            data = answer("项目约定使用绝对导入，依据 guide.md:1。")
        return httpx.Response(200, json=data)

    def child(request):
        body = json.loads(request.content)
        child_requests.append(body)
        if len(child_requests) == 1:
            data = model_response(call_search("配置导入要求"), tool=True)
        else:
            evidence = outputs(body)[-1]
            assert evidence["results"][0]["path"] == str(source)
            assert evidence["results"][0]["start_line"] == 1
            assert "绝对导入" in evidence["results"][0]["text"]
            data = answer("guide.md:1 要求绝对导入。")
        return httpx.Response(200, json=data)

    with application(tmp_path, parent, child, config=config_at(tmp_path)) as (app, store):
        assert "绝对导入" in app.send("请子任务查配置文档")
        assert len(child_requests) == 2
        assert app.usage().tokens > 0


def test_cached_external_document_cannot_bypass_revoked_permission(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "private.md").write_text("范围受限的项目资料", encoding="utf-8")
    config = DocumentConfig(roots=(external,))
    cache = tmp_path / "cache"
    allowed = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(external,)))
    before = DocumentIndex(cache, workspace, config, allowed.resolve)
    assert DocumentSearch(before).search("项目资料", 3).hits
    snapshot = before.path.read_bytes()
    denied = PathPolicy(workspace, PermissionConfig())
    after = DocumentIndex(cache, workspace, config, denied.resolve)
    with pytest.raises(ToolError, match="未授权"):
        DocumentSearch(after).search("项目资料", 3)
    assert after.path.read_bytes() == snapshot


def test_cancelled_retrieval_preserves_snapshots_and_stops_embedding(tmp_path):
    token = CancellationToken()
    config = DocumentConfig(roots=(tmp_path / "docs",), method="semantic")
    (tmp_path / "docs").mkdir()
    source = tmp_path / "docs" / "network.md"
    source.write_text("网络规则", encoding="utf-8")
    index = DocumentIndex(
        tmp_path / "cache",
        tmp_path,
        config,
        PathPolicy(tmp_path, PermissionConfig()).resolve,
        token.check,
    )
    backend = FakeEmbedding()
    search = DocumentSearch(index, backend)
    search.search("问题", 3)
    snapshot, vectors = index.path.read_bytes(), search.vectors.path.read_bytes()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        search.search("问题", 3)
    assert index.path.read_bytes() == snapshot
    assert search.vectors.path.read_bytes() == vectors
    assert len(backend.inputs) == 1


def test_cancellation_during_embedding_does_not_commit_partial_vectors(tmp_path):
    index = make_index(tmp_path)
    index.config = replace(index.config, method="semantic")
    token = CancellationToken()
    index.check_cancel = token.check
    (tmp_path / "docs" / "network.md").write_text("网络规则\n\n网络恢复", encoding="utf-8")

    class CancellingEmbedding(FakeEmbedding):
        def passages(self, texts):
            result = super().passages(texts)
            token.cancel()
            return result

    search = DocumentSearch(index, CancellingEmbedding())
    with pytest.raises(RequestInterrupted):
        search.search("问题", 3)
    assert not search.vectors.path.exists()


def test_vector_save_failure_is_visible_and_retry_reuses_valid_source(tmp_path, monkeypatch):
    index = make_index(tmp_path)
    index.config = replace(index.config, method="semantic")
    source = tmp_path / "docs" / "network.md"
    source.write_text("网络旧规则", encoding="utf-8")
    search = DocumentSearch(index, FakeEmbedding())
    search.search("问题", 3)
    previous = search.vectors.path.read_bytes()
    source.write_text("网络新规则", encoding="utf-8")

    def failed_save(*args):
        # 模拟向量快照落盘失败，文本索引仍按真实流程成功刷新。
        raise OSError("vector cache disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("adam.documents.vectors.save_snapshot", failed_save)
        with pytest.raises(DocumentError, match="未返回旧资料"):
            search.search("问题", 3)
    assert search.vectors.path.read_bytes() == previous
    assert "新规则" in search.search("问题", 3).hits[0].chunk.text


@pytest.mark.parametrize(
    "limits",
    [
        {"max_file_bytes": 1},
        {"max_total_bytes": 1},
        {"max_scan_entries": 1},
        {"max_chunks": 1},
    ],
)
def test_resource_limits_preserve_old_index_instead_of_partial_results(tmp_path, limits):
    index = make_index(tmp_path)
    for name in ("a.md", "b.md"):
        (tmp_path / "docs" / name).write_text("有效文档\n\n第二段", encoding="utf-8")
    index.refresh()
    previous = index.path.read_bytes()
    limited = make_index(tmp_path, replace(index.config, **limits))
    with pytest.raises(DocumentError, match="超限|上限|过大"):
        limited.refresh()
    assert limited.path.read_bytes() == previous


def test_relative_workspace_and_no_evidence_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "project" / "docs").mkdir(parents=True)
    (tmp_path / "project" / "docs" / "guide.md").write_text("项目导入约定", encoding="utf-8")
    config = replace(config_at(tmp_path), documents=DocumentConfig(roots=(Path("docs"),)))
    payloads = []

    def handle(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            response = model_response(call_search("火星氧气配给"), tool=True)
        else:
            result = outputs(body)[-1]
            assert result["ok"] and result["status"] == "no_match"
            response = model_response(
                {"role": "assistant", "content": "文档中未找到依据，无法确定。"}
            )
        return httpx.Response(200, json=response)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), Path("project"))
        assert "未找到依据" in app.send("火星氧气配给")
        assert len(payloads) == 2

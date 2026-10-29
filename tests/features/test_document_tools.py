"""E2.3：真实文档搜索经过模型工具闭环，假 HTTP 只替代远端模型。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.loader import load_config
from adam.config.settings import AppConfig, ContextConfig, DocumentConfig, LLMConfig, ToolLimits
from adam.contracts.errors import BudgetError, ConfigError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.document_tools import register_documents
from adam.tool_calls.factory import create_tool_registry


def config_at(root, **kwargs):
    return AppConfig(
        LLMConfig("https://example.test", "test", "unused", stream=False),
        root / "sessions",
        "只根据证据回答",
        documents=DocumentConfig(roots=(root / "docs",)),
        context=ContextConfig(auto_compact=False),
        **kwargs,
    )


def model_response(message, *, tool=False):
    return {
        "choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def call_search(query):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "doc1",
                "type": "function",
                "function": {"name": "search_documents", "arguments": json.dumps({"query": query})},
            }
        ],
    }


def test_search_evidence_citation_and_persisted_tool_result(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "network.md"
    source.write_text("# 网络\n\n连接失败最多重试 2 次。", encoding="utf-8")
    payloads = []
    cited_reply = f"最多重试 2 次。依据：{source}:3"
    replies = iter(
        [
            model_response(call_search("连接失败重试次数"), tool=True),
            model_response({"role": "assistant", "content": cited_reply}),
        ]
    )

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    config = config_at(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        assert app.send("失败后重试几次？") == cited_reply
        session_id = app.session.session_id
        result = json.loads(payloads[1]["messages"][-1]["content"])
        item = next(item for item in result["results"] if "2 次" in item["text"])
        assert item["path"] == str(source)
        assert item["start_line"] == item["end_line"] == 3
        assert len(item["version"]) == 64
        assert "search_documents" in {t["function"]["name"] for t in payloads[0]["tools"]}
        events = store.load_events(session_id)
        assert any(item["version"] in json.dumps(event, ensure_ascii=False) for event in events)
        # 修改源文件不应篡改已经落盘的历史证据。
        source.write_text("现在最多重试 3 次。", encoding="utf-8")
        assert item["version"] in json.dumps(store.load_events(session_id), ensure_ascii=False)
        assert any("2 次" in str(message) for message in store.load_messages(session_id))


def test_no_match_failure_and_complete_json_output_budget(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "guide.md"
    source.write_text("测试相关文档\n" + "详细说明" * 100, encoding="utf-8")
    config = DocumentConfig(roots=(docs,))
    permissions = config_at(tmp_path).permissions
    registry = register_documents(
        create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=512)),
        tmp_path,
        config,
        tmp_path / "cache",
        permissions,
    )
    unknown = json.loads(registry.execute("search_documents", '{"query":"火星氧气"}'))
    assert unknown["ok"] and unknown["status"] == "no_match"
    output = registry.execute("search_documents", '{"query":"详细说明"}')
    assert len(output) <= 512
    value = json.loads(output)
    assert value["status"] == "output_limit" and value["truncated"]
    source.write_bytes(b"\xff")
    failure = json.loads(registry.execute("search_documents", '{"query":"文档"}'))
    assert failure["ok"] is False and "UTF-8" in failure["error"]


def test_ordinary_chat_does_not_scan_or_create_index(tmp_path):
    config = config_at(tmp_path)
    # 已配置但不存在的文档目录不会影响无需查资料的普通对话。
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json=model_response({"role": "assistant", "content": "你好"})
                )
            )
        ) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        assert app.send("你好") == "你好"
        assert not (config.sessions_dir / "_documents").exists()


def test_document_config_paths_and_invalid_fields(tmp_path):
    main = tmp_path / "config.toml"
    llm = tmp_path / "llm.toml"
    llm.write_text('[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="unused"')
    main.write_text('[documents]\nroots=["docs"]\ntop_k=3')
    loaded = load_config(main)
    assert loaded.documents.roots == (Path("docs"),)
    assert loaded.documents_dir == tmp_path / "documents"
    assert loaded.documents.top_k == 3
    for invalid in ('roots="docs"', 'roots=[""]', "top_k=true", "unknown=1"):
        main.write_text("[documents]\n" + invalid)
        with pytest.raises(ConfigError):
            load_config(main)


def test_document_results_use_existing_context_budget(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "large.md").write_text(("权限配置 " * 60 + "\n\n") * 10, encoding="utf-8")
    config = config_at(tmp_path)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=model_response(call_search("权限配置"), tool=True))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        initial = app.preview_context(query="查权限配置").estimate.input_tokens
        small = replace(
            config,
            context=ContextConfig(
                auto_compact=False, max_input_tokens=initial + 500, safety_margin_tokens=0
            ),
        )
        app = build_application(small, store, LLMClient(config.llm, http), tmp_path)
        with pytest.raises(BudgetError):
            app.send("查权限配置")
    assert len(payloads) == 1


def test_cache_is_not_exposed_through_raw_child_file_tools(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("文档规则", encoding="utf-8")
    cache = tmp_path / "cache"
    config = DocumentConfig(roots=(docs,))
    registry = register_documents(
        create_tool_registry(tmp_path, delegated=True, protected_dirs=(cache,)),
        tmp_path,
        config,
        cache,
        config_at(tmp_path).permissions,
    )
    assert json.loads(registry.execute("search_documents", '{"query":"文档"}'))["ok"]
    cached = next(cache.glob("*.json"))
    output = registry.execute("read_file", json.dumps({"path": str(cached)}))
    assert json.loads(output)["ok"] is False

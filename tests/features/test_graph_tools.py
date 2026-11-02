"""C08.3：原注册表执行图工具，保留模式、边界和完整引用结构。"""

import json

from adam.config.settings import ToolLimits
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.documents.search import DocumentSearch
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.graph_tools import register_graph
from tests.features.test_graph_evidence import corpus_at


def test_graph_tool_uses_original_registry_and_keeps_complete_sources(tmp_path):
    index, _ = corpus_at(tmp_path)
    registry = register_graph(create_tool_registry(tmp_path), DocumentSearch(index)).with_mode(
        lambda: ExecutionMode.PLAN
    )
    assert registry.capability("search_graph_documents") is ToolCapability.READ_ONLY
    assert not registry.can_parallelize("search_graph_documents")
    args = {"query": "Checkout", "source_id": "module:checkout", "target_id": "team:billing"}
    output = json.loads(registry.execute("search_graph_documents", json.dumps(args)))
    assert output["ok"] and len(output["paths"]) == 1 and len(output["paths"][0]) == 2
    assert {c["methods"][0] for c in output["candidates"]} == {"text", "graph"}
    assert all(e["evidence"]["version"] and e["evidence"]["text"] for e in output["paths"][0])
    bad = json.loads(
        registry.execute("search_graph_documents", json.dumps({**args, "path": "secret"}))
    )
    assert not bad["ok"]


def test_graph_tool_output_limit_and_ambiguous_label_never_deliver_fake_path(tmp_path):
    index, _ = corpus_at(tmp_path)
    base = create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=1000))
    registry = register_graph(base, DocumentSearch(index))
    args = {"query": "Checkout", "source_id": "module:checkout", "target_id": "team:billing"}
    limited = json.loads(registry.execute("search_graph_documents", json.dumps(args)))
    assert not limited["ok"] and "paths" not in limited
    unknown = json.loads(
        registry.execute("search_graph_documents", json.dumps({**args, "source_id": "Checkout"}))
    )
    assert not unknown["ok"] and "paths" not in unknown

"""B16.2：语义候选接入真实发现/执行/记录流程；LLM和向量可控，本地文件实际操作。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.settings import ToolDiscoveryConfig, ToolLimits
from adam.contracts.errors import ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.tool_catalog import tool_schema_record
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery_tools import register_discovery
from adam.tool_calls.registry import ToolRegistry
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import names
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


class Vectors:
    """只给 read_file 或全部目录高分；记录真实输入以检查主子和模式过滤。"""

    def __init__(self, *, all_match=False, fault=None):
        self.identity = "semantic-tool-flow-v1"
        self.all_match, self.fault = all_match, fault
        self.batches, self.queries = [], []

    def passages(self, texts):
        self.batches.append(list(texts))
        if self.fault == "partial":
            return []
        return [
            [1.0, 0.0] if self.all_match or text.startswith("read_file ") else [0.0, 1.0]
            for text in texts
        ]

    def query(self, text):
        self.queries.append(text)
        if self.fault == "error":
            raise RuntimeError("PRIVATE_BACKEND_ERROR")
        return [1.0, 0.0]


def semantic_config(root):
    return replace(config_at(root), tool_discovery=ToolDiscoveryConfig(ranking="semantic"))


def install_backend(monkeypatch, backend):
    """组装仅创建本地后端，查询前不得编码；不替换发现服务或工具执行。"""
    created = []

    def create(cache, model):
        created.append((cache, model))
        return backend

    monkeypatch.setattr("adam.bootstrap.FastEmbedBackend", create)
    return created


def test_semantic_list_discover_actual_request_read_restart_and_offline_replay(
    tmp_path, monkeypatch
):
    (tmp_path / "note.txt").write_text("evidence=42", encoding="utf-8")
    backend = Vectors()
    created = install_backend(monkeypatch, backend)
    config = semantic_config(tmp_path)

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            result = tool_response([call("list", "list_tools", query="查看项目文件的内容")])
        elif number == 2:
            found = outputs(body)[-1]
            assert found["ranking"]["strategy"] == "semantic_v1"
            assert found["tools"][0]["name"] == "read_file" and found["tools"][0]["score"] == 1.0
            assert names(body) == ["list_tools", "discover_tools"]
            result = tool_response(
                [
                    call("discover", "discover_tools", names=["read_file"]),
                    call("too_early", "read_file", path="note.txt"),
                ]
            )
        elif number == 3:
            assert "read_file" in names(body)
            assert not outputs(body)[-1]["ok"] and "尚未提供" in outputs(body)[-1]["error"]
            result = tool_response([call("actual", "read_file", path="note.txt")])
        else:
            assert outputs(body)[-1]["lines"][0]["text"] == "evidence=42"
            result = answer("42")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, bodies):
        assert backend.batches == []
        assert created == [
            (config.sessions_dir / "_documents/models", config.tool_discovery.embedding_model)
        ]
        assert recorder.run(lambda: app.send("task")) == "42"
        sid = app.session.session_id
        events = store.load_events(sid)
        requests = [event["payload"] for event in events if event["type"] == "request_started"]
        assert [r["context"]["tool_schema"] for r in requests] == [
            tool_schema_record(b["tools"]) for b in bodies
        ]
        assert len({r["turn_id"] for r in requests}) == 1
        assert "semantic-tool-flow-v1" in json.dumps(events)
    with SessionStore(config.sessions_dir) as store:
        store.select_session(sid)
        assert store.load_events(sid) == events
    assert len(backend.queries) == 1 and len(backend.batches) == 1
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        "adam.tool_calls.semantic_discovery.SemanticToolRanker.rank",
        lambda *a, **k: pytest.fail("offline replay encoded tools"),
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


def test_plan_filters_before_semantic_encoding_and_found_tool_still_respects_paths(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("PRIVATE_OUTSIDE", encoding="utf-8")
    backend = Vectors(all_match=True)
    install_backend(monkeypatch, backend)
    config = semantic_config(root)
    original = Path.open

    def checked(path, *args, **kwargs):
        if path == outside:
            pytest.fail("discovered tool bypassed path policy")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked)

    def respond(body, number):
        assert "PRIVATE_OUTSIDE" not in json.dumps(body)
        if number == 1:
            result = tool_response([call("list", "list_tools", query="文件操作")])
        elif number == 2:
            assert not {"replace_text", "run_command"} & {
                e["name"] for e in outputs(body)[-1]["tools"]
            }
            result = tool_response([call("discover", "discover_tools", names=["read_file"])])
        elif number == 3:
            result = tool_response([call("outside", "read_file", path=str(outside))])
        else:
            assert not outputs(body)[-1]["ok"]
            result = answer("范围外未读取")
        return httpx.Response(200, json=result)

    with captured_app(root, respond, config=config) as (app, recorder, _, _, _):
        app.select_mode("plan")
        assert recorder.run(lambda: app.send("task")) == "范围外未读取"
    assert all(
        not text.startswith(("replace_text ", "run_command ")) for text in backend.batches[0]
    )


def fixture_registry(limit=8000):
    specs = {f"tool_{i}": schema("说明" * 40, {}, []) for i in range(12)}
    return ToolRegistry(
        specs,
        dict.fromkeys(specs, lambda *_: pytest.fail("query executed tool")),
        ToolLimits(max_result_chars=limit),
        capabilities=dict.fromkeys(specs, ToolCapability.READ_ONLY),
        groups=dict.fromkeys(specs, "examples"),
    )


@pytest.mark.parametrize("fault", ["missing", "error", "partial"])
def test_query_failure_reports_fallback_and_keeps_exact_name_and_unselected_state(fault):
    registry, service = register_discovery(
        fixture_registry(),
        ToolDiscoveryConfig(ranking="semantic"),
        lambda: ExecutionMode.ACT,
        embedding_backend=None if fault == "missing" else Vectors(fault=fault),
    )
    service.definitions("turn")
    result = json.loads(registry.execute("list_tools", '{"query":"tool_3"}'))
    assert result["ok"] and result["tools"][0]["name"] == "tool_3"
    assert result["ranking"]["strategy"] == "keyword_fallback_v1"
    assert result["ranking"]["fallback_reason"] == "embedding_unavailable"
    assert result["ranking"]["embedding"] is None and "PRIVATE_BACKEND_ERROR" not in json.dumps(
        result
    )
    assert [t["function"]["name"] for t in service.definitions("turn")] == [
        "list_tools",
        "discover_tools",
    ]


def test_semantic_pages_include_provenance_without_truncation_or_lost_candidates():
    backend = Vectors(all_match=True)
    registry, service = register_discovery(
        fixture_registry(800),
        ToolDiscoveryConfig(ranking="semantic"),
        lambda: ExecutionMode.ACT,
        embedding_backend=backend,
    )
    service.definitions("turn")
    found, offset, versions = [], 0, set()
    while offset is not None:
        raw = registry.execute(
            "list_tools", json.dumps({"query": "任务描述", "offset": offset, "limit": 100})
        )
        page = json.loads(raw)
        assert page["ok"] and len(raw) <= 800 and not page.get("truncated")
        found.extend(t["name"] for t in page["tools"])
        versions.add(page["ranking"]["directory_version"])
        offset = page["next_offset"]
    assert found == [f"tool_{i}" for i in range(12)] and len(versions) == 1
    assert len(backend.batches) == 1


def test_metadata_too_large_for_page_fails_explicitly_and_new_catalog_rebuilds_index():
    backend = Vectors(all_match=True)
    _, small = register_discovery(
        fixture_registry(128),
        ToolDiscoveryConfig(ranking="semantic"),
        lambda: ExecutionMode.ACT,
        embedding_backend=backend,
    )
    small.definitions("first")
    with pytest.raises(ToolError, match="上限"):
        small.list_tools({"query": "描述"}, None)
    registry = fixture_registry().extended(
        {"new_query": schema("新工具", {}, [])},
        {"new_query": lambda *_: {}},
        capabilities={"new_query": ToolCapability.READ_ONLY},
        groups={"new_query": "examples"},
    )
    registry, newer = register_discovery(
        registry,
        ToolDiscoveryConfig(ranking="semantic"),
        lambda: ExecutionMode.ACT,
        embedding_backend=backend,
    )
    newer.definitions("second")
    result = json.loads(registry.execute("list_tools", '{"query":"新工具"}'))
    assert len(result["tools"]) == 13 and any(t["name"] == "new_query" for t in result["tools"])
    assert len(backend.batches) == 2 and small.directory.version != newer.directory.version


def test_parent_and_real_child_use_separate_rankers_and_child_cannot_discover_delegation(
    tmp_path, monkeypatch
):
    (tmp_path / "note.txt").write_text("child evidence", encoding="utf-8")
    backends = []

    def create(cache, model):
        backend = Vectors()
        backends.append(backend)
        return backend

    monkeypatch.setattr("adam.bootstrap.FastEmbedBackend", create)
    child_calls = []

    def child(request):
        body = json.loads(request.content)
        child_calls.append(body)
        number = len(child_calls)
        if number == 1:
            result = tool_response([call("list", "list_tools", query="查看文件")])
        elif number == 2:
            assert outputs(body)[-1]["ranking"]["strategy"] == "semantic_v1"
            result = tool_response([call("discover", "discover_tools", names=["read_file"])])
        elif number == 3:
            result = tool_response([call("read", "read_file", path="note.txt")])
        else:
            assert outputs(body)[-1]["lines"][0]["text"] == "child evidence"
            result = answer("child checked")
        return httpx.Response(200, json=result)

    def respond(body, number):
        if number == 1:
            result = tool_response([call("list", "list_tools", query="查看文件")])
        elif number == 2:
            result = tool_response(
                [call("discover", "discover_tools", names=["spawn_agent", "wait_agents"])]
            )
        elif number == 3:
            result = tool_response(
                [call("spawn", "spawn_agent", name="reader", task="读取note.txt")]
            )
        elif number == 4:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            result = answer("checked with child")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=semantic_config(tmp_path), child_handler=child) as (
        app,
        recorder,
        _,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "checked with child"
    assert len(backends) == 2 and all(len(b.batches) == 1 for b in backends)
    assert any(t.startswith("spawn_agent ") for t in backends[0].batches[0])
    assert not any(t.startswith(("spawn_agent ", "wait_agents ")) for t in backends[1].batches[0])

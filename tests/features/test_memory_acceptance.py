"""E1.5：固定输入对照和运行时生命周期验收；不评价真实模型回答质量。"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from filelock import FileLock

import adam.application.memories as memories_module
from adam.application.memories import MemoryService
from adam.bootstrap import build_application
from adam.cli.main import handle_command
from adam.config.settings import MemoryConfig, PermissionConfig
from adam.context.builder import ContextBuilder
from adam.contracts.errors import MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemorySelection, MemorySource
from adam.llm.client import LLMClient
from adam.memory.project import project_identity
from adam.memory.store import MemoryStore
from adam.session.storage import SessionStore
from tests.features.test_memory_context import completion, config_at, memory_records


def test_all_injection_vs_relevant_selection_uses_identical_inputs(tmp_path):
    """全部注入基线同样过滤项目与过期；只比较选择造成的差异。"""
    config = config_at(tmp_path)

    def clock():
        return datetime(2026, 10, 29, tzinfo=UTC)

    database = MemoryStore(config.memories_dir, clock=clock)
    project = project_identity(tmp_path)
    source = MemorySource("add")
    python = database.add("Python uses absolute imports", project, source)
    renderer = database.add("renderer keeps GPU evidence", project, source)
    database.add("lunch prefers noodles", project, source)
    language = database.add("language replies in Chinese", None, source)
    database.add("Python wrong project", str(tmp_path / "other"), source)
    database.add("Python expired", project, source, expires_at="2020-01-01T00:00:00Z")
    scoped = tuple(
        r for r in database.list_records(project, include_global=True) if r.active(clock())
    )
    payloads, results = [], []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        for query, expected in [
            ("Python imports", {python.memory_id}),
            ("renderer language", {renderer.memory_id, language.memory_id}),
            ("weather", set()),
        ]:
            app.new(query)
            preview = app.preview_context(query)
            baseline = ContextBuilder(config.context, config.agent.max_output_tokens).build(
                model=config.llm.model,
                system_prompt=config.system_prompt,
                history=[],
                current_turn=[{"role": "user", "content": query}],
                tools=preview.tools,
                memory=MemorySelection(scoped, query, "selected"),
            )
            app.send(query)
            actual = payloads[-1]
            assert actual["messages"] == preview.messages and actual["tools"] == preview.tools
            assert {r["memory_id"] for r in memory_records(actual)} == expected
            assert actual["messages"][0] == baseline.messages[0]
            assert actual["messages"][-1] == baseline.messages[-1]
            assert baseline.estimate.memory_tokens > preview.estimate.memory_tokens
            assert preview.estimate.memory_tokens <= config.memory.max_input_tokens
            preview.ensure_within_budget()
            results.append(
                {
                    "query": query,
                    "all_count": len(scoped),
                    "selected_contents": [r["content"] for r in memory_records(actual)],
                    "all_memory_tokens": baseline.estimate.memory_tokens,
                    "selected_memory_tokens": preview.estimate.memory_tokens,
                    "all_input_tokens": baseline.estimate.input_tokens,
                    "selected_input_tokens": preview.estimate.input_tokens,
                }
            )
    report = json.dumps(results, ensure_ascii=False, indent=2)
    (tmp_path / "comparison.json").write_text(report, encoding="utf-8")
    print(report)


def test_expiry_update_and_delete_across_application_restart(tmp_path, monkeypatch):
    """固定时钟跨过有效期，再重建应用；普通模型回答不会写回记忆库。"""

    class Clock:
        current = datetime(2030, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(memories_module, "datetime", Clock)
    config = config_at(tmp_path)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        handle_command(
            "/memory add --expires 2030-01-02T00:00:00Z -- Python temporary", app, lambda _: None
        )
        item = app.manage_memory(MemoryCommand("list"))[0]
        path = config.memories_dir / "memories.json"
        before = path.read_bytes()
        app.send("Python")
        assert memory_records(bodies[-1])[0]["memory_id"] == item.memory_id
        Clock.current = datetime(2030, 1, 3, tzinfo=UTC)
        app.send("Python")
        assert memory_records(bodies[-1]) == []
        assert path.read_bytes() == before
        handle_command(
            f"/memory update {item.memory_id} --revision 1 -- Python permanent", app, lambda _: None
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.send("Python")
        recalled = memory_records(bodies[-1])
        assert recalled[0]["revision"] == 2 and recalled[0]["expires_at"] is None
        handle_command(f"/memory delete {item.memory_id} --revision 2", app, lambda _: None)
        app.new("after delete")
        app.send("Python")
        assert memory_records(bodies[-1]) == []
        history = app.manage_memory(MemoryCommand("list", memory_id=item.memory_id, history=True))
        assert [record.revision for record in history] == [1, 2, 3]
        assert history[-1].deleted


def test_lock_contention_is_visible_and_fsync_failure_preserves_previous_file(
    tmp_path, monkeypatch
):
    database = MemoryStore(tmp_path / "memory", lock_timeout=0.01)
    database.add("Python original", None, MemorySource("add"))
    before = database.path.read_bytes()
    service = MemoryService(database, tmp_path)
    with FileLock(database.root / "memories.lock"):
        selection = service.select("Python", 1000)
        assert selection.status == "unavailable" and selection.warning
        with pytest.raises(MemoryStoreError):
            service.manage(MemoryCommand("add", "not saved", global_scope=True))
    assert database.path.read_bytes() == before

    def fail(_fd):
        raise OSError("injected fsync failure before replacement")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(MemoryStoreError):
            service.manage(MemoryCommand("add", "not durable", global_scope=True))
    assert database.path.read_bytes() == before
    assert not list(database.root.glob("*.tmp"))
    assert service.select("Python", 1000).status == "selected"


def test_extra_read_permission_does_not_expand_memory_scope(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    database = MemoryStore(tmp_path / "memory")
    database.add("Python project A", project_identity(a), MemorySource("add"))
    config = replace(
        config_at(b, memories_dir=database.root),
        permissions=PermissionConfig(additional_read_dirs=(a,)),
    )

    def handler(_request):
        raise AssertionError("preview does not call a model")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), b)
        assert app.preview_context("Python").memory.status == "no_match"


def test_disabled_memory_never_reads_corrupt_store(tmp_path):
    config = config_at(tmp_path, memory=MemoryConfig(enabled=False))
    config.memories_dir.mkdir()
    (config.memories_dir / "memories.json").write_text("broken", encoding="utf-8")
    payloads, notices = [], []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, on_notice=notices.append
        )
        assert app.send("Python") == "answer"
        assert app.preview_context("Python").memory.status == "disabled"
        assert memory_records(payloads[0]) == [] and notices == []

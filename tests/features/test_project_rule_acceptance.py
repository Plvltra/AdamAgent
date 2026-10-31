"""T1.3：上下文展示、持久快照、重启与故障边界的完整业务验收。"""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import CommandResult, handle_command
from adam.config.loader import load_config
from adam.config.settings import ContextConfig
from adam.contracts.errors import BudgetError, ConfigError, StorageError
from adam.contracts.rules import validate_rules
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_project_rule_execution import command, rule_text
from tests.features.test_project_rules import write_rule
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.test_config import LLM


def contexts(store, sid):
    return [
        item["payload"]["context"]
        for item in store.load_events(sid)
        if item["type"] == "request_started"
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_context_shows_source_version_and_cost_without_creating_session(tmp_path, encoding):
    write_rule(tmp_path, "PRIVATE_RULE_BODY")
    (tmp_path / "AGENTS.md").write_text("PRIVATE_RULE_BODY", encoding=encoding)
    write_rule(tmp_path / "sub", "CHILD_NOT_DISCOVERED")
    config = config_at(tmp_path)
    shown = []

    def never(_):
        pytest.fail("context preview must not call HTTP")

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(never)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        preview = app.preview_context()
        validate_rules(preview.rules.to_record())
        assert handle_command("/context", app, shown.append) == CommandResult.HANDLED
        text = "\n".join(shown)
        assert "项目规则" in text and f"项目规则 {preview.estimate.rules_tokens}" in text
        assert "AGENTS.md" in text and preview.rules.documents[0].version[:12] in text
        assert "PRIVATE_RULE_BODY" not in text
        assert "CHILD_NOT_DISCOVERED" not in text
        assert "根规则" in text and "子目录" in text
        shown.clear()
        handle_command("/context full", app, shown.append)
        assert "PRIVATE_RULE_BODY" in "\n".join(shown)
        assert not store.list_sessions() and not list(config.sessions_dir.rglob("*.jsonl"))


def test_sessions_and_restart_use_current_rules_but_preserve_old_request_snapshots(tmp_path):
    write_rule(tmp_path, "ROOT_OLD")
    write_rule(tmp_path / "sub", "CHILD_OLD")
    target = tmp_path / "sub" / "data.txt"
    target.write_text("evidence", encoding="utf-8")
    config = config_at(tmp_path)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200, json=tool_response([call("read", "read_file", path="sub/data.txt")])
            )
        if len(requests) == 2:
            assert outputs(body)[-1]["status"] == "deferred"
            assert "CHILD_OLD" in rule_text(body)
            return httpx.Response(
                200, json=tool_response([call("retry", "read_file", path="sub/data.txt")])
            )
        if len(requests) == 3:
            assert outputs(body)[-1]["ok"]
        else:
            assert "CHILD_OLD" not in rule_text(body)
        return httpx.Response(200, json=answer("done"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        assert app.send("读取子目录") == "done"
        first = app.session.session_id
        old = deepcopy(contexts(store, first))
        app.new("B")
        assert app.send("普通问答") == "done"
        assert len(contexts(store, app.session.session_id)[0]["rules"]["documents"]) == 1
        app.resume(first)
        assert [doc.scope for doc in app.preview_context().rules.documents] == ["."]
        # 运行状态不能因切回旧会话恢复；旧请求证据独立保存。
        app.close()
    write_rule(tmp_path, "ROOT_NEW")
    (tmp_path / "sub" / "AGENTS.md").unlink()

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.resume(first)
        assert contexts(store, first) == old
        assert app.preview_context().rules.documents[0].content == "ROOT_NEW"
        assert app.send("恢复后继续") == "done"
        assert "ROOT_NEW" in rule_text(requests[-1]) and "ROOT_OLD" not in rule_text(requests[-1])
        assert contexts(store, first)[:3] == old
        assert len(store.load_turns(first)) == 2
        app.close()


def test_new_scope_budget_failure_keeps_diagnostic_input_and_zero_side_effects(tmp_path):
    write_rule(tmp_path / "sub", "LARGE_RULE\n" * 900)
    marker = tmp_path / "not-run"
    config = config_at(tmp_path)
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1
        return httpx.Response(200, json=tool_response([command(marker, cwd="sub")]))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        client = LLMClient(config.llm, http)
        baseline = build_application(config, store, client, tmp_path).preview_context("执行")
        bounded = replace(
            config,
            context=ContextConfig(
                max_input_tokens=baseline.estimate.input_tokens + 700,
                safety_margin_tokens=100,
                auto_compact=False,
            ),
        )
        app = build_application(
            bounded,
            store,
            client,
            tmp_path,
            confirm_command=lambda _: pytest.fail("over-budget rule must not reach approval"),
        )
        with pytest.raises(BudgetError):
            app.send("执行")
        sid = app.session.session_id
        assert len(requests) == 1 and len(contexts(store, sid)) == 1
        assert not marker.exists() and not store.load_executions(sid)
        assert not store.load_turns(sid)
        turns = [
            item["payload"] for item in store.load_events(sid) if item["type"] == "turn_started"
        ]
        assert turns[0]["user_text"] == "执行"
        assert store.usage(sid)[1] is False


@pytest.mark.parametrize("bad", ["scope", "duplicate", "version", "size"])
def test_invalid_saved_rule_snapshot_is_rejected_without_repairing_complete_records(tmp_path, bad):
    write_rule(tmp_path, "ROOT")
    config = config_at(tmp_path)
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer("done")))
        ) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.send("hello")
        sid = app.session.session_id
        events = store.load_events(sid)
        app.close()
    record = next(item for item in events if item["type"] == "request_started")
    documents = record["payload"]["context"]["rules"]["documents"]
    if bad == "scope":
        documents[0]["scope"] = "../outside"
    elif bad == "duplicate":
        documents.append(deepcopy(documents[0]))
    elif bad == "version":
        documents[0]["version"] = "0" * 64
    else:
        documents[0]["content"] = "x" * 16385
        documents[0]["version"] = sha256(documents[0]["content"].encode("utf-8")).hexdigest()
    path = next(config.sessions_dir.glob(f"*/*/*/{sid}.jsonl"))
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8"
    )
    original = path.read_bytes()
    with SessionStore(config.sessions_dir) as store:
        with pytest.raises(StorageError):
            store.select_session(sid)
    assert path.read_bytes() == original
    assert not list(config.sessions_dir.rglob("*.bak"))


@pytest.mark.parametrize("section", ["rules=1", "[rules]\nenabled=1", "[rules]\nunknown=true"])
def test_rules_config_rejects_unknown_or_non_boolean_fields(tmp_path, section):
    (tmp_path / "config.toml").write_text(section, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.toml")


def test_disabled_rules_skip_invalid_source_and_keep_existing_tool_loop(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[rules]\nenabled=false\n[tool_discovery]\nmode="full"\n', encoding="utf-8"
    )
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")
    (tmp_path / "data.txt").write_text("evidence", encoding="utf-8")
    config = load_config(tmp_path / "config.toml")
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert not rule_text(body)
        if len(requests) == 1:
            return httpx.Response(
                200, json=tool_response([call("read", "read_file", path="data.txt")])
            )
        assert outputs(body)[-1]["ok"]
        return httpx.Response(200, json=answer("done"))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        assert app.preview_context().estimate.rules_tokens == 0
        assert app.send("读取") == "done"
        assert len(requests) == 2
        assert all("rules" not in item for item in contexts(store, app.session.session_id))

"""T12.2：模拟远端 HTTP，验证生产发现、固定请求名单、额度与 JSONL。"""

import json
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import (
    AppConfig,
    ContextConfig,
    LLMConfig,
    ReflectionConfig,
    ToolDiscoveryConfig,
)
from adam.contracts.errors import ApprovalDeniedError, BudgetError, StorageError
from adam.contracts.tool_catalog import tool_schema_record
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def settings(root, **changes):
    """使用真实默认按需模式，关闭自动反思以固定故障场景的请求数。"""
    return replace(
        AppConfig(
            LLMConfig("https://unused.test", "test", "unused", stream=False),
            root / "sessions",
            "",
            reflection=ReflectionConfig(enabled=False),
            context=ContextConfig(auto_compact=False),
        ),
        **changes,
    )


@contextmanager
def application(root, respond, *, config=None, approve=None):
    bodies = []
    config = config or settings(root)

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=respond(body, len(bodies)))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            root,
            confirm_edit=approve or (lambda _: pytest.fail("unexpected approval")),
        )
        try:
            yield app, store, bodies
        finally:
            app.close()


def names(body):
    return [item["function"]["name"] for item in body["tools"]]


def test_discover_read_answer_and_actual_request_metadata(tmp_path):
    (tmp_path / "note.txt").write_text("证据=42", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            return tool_response([call("discover", "discover_tools", names=["read_file"])])
        if number == 2:
            assert names(body) == ["list_tools", "discover_tools", "read_file"]
            assert outputs(body)[-1]["added_count"] == 1
            return tool_response([call("read", "read_file", path="note.txt")])
        assert "证据=42" in json.dumps(outputs(body), ensure_ascii=False)
        return answer("42")

    with application(tmp_path, respond) as (app, store, bodies):
        assert app.send("查 note.txt") == "42"
        events = store.load_events(app.session.session_id)
        requests = [e["payload"] for e in events if e["type"] == "request_started"]
        assert [r["context"]["tool_schema"] for r in requests] == [
            tool_schema_record(body["tools"]) for body in bodies
        ]
        assert len({r["turn_id"] for r in requests}) == 1
        tools = [e["payload"] for e in events if e["type"] == "tool_started"]
        assert [t["request_id"] for t in tools] == [r["request_id"] for r in requests[:2]]
        sid = app.session.session_id
    with SessionStore(settings(tmp_path).sessions_dir) as reopened:
        assert reopened.load_events(sid) == events


def test_same_batch_discovery_does_not_allow_early_edit_or_approval(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    approvals = []

    def respond(body, number):
        if number == 1:
            return tool_response(
                [
                    call("discover", "discover_tools", names=["replace_text"]),
                    call("early", "replace_text", path="target.txt"),
                ]
            )
        assert names(body) == ["list_tools", "discover_tools", "replace_text"]
        result = outputs(body)[-1]
        assert not result["ok"] and "本次请求尚未提供" in result["error"]
        return answer("提前调用已拒绝")

    with application(tmp_path, respond, approve=approvals.append) as (app, store, _):
        assert app.send("修改") == "提前调用已拒绝"
        assert not any(
            e["type"].startswith("edit_") for e in store.load_events(app.session.session_id)
        )
    assert approvals == [] and target.read_text(encoding="utf-8") == "before"


def test_hidden_read_never_reads_target_and_can_recover(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path / "secret.txt"
    target.write_text("private", encoding="utf-8")
    original = Path.open

    def protect(path, *args, **kwargs):
        if path == target:
            pytest.fail("hidden tool accessed the target")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protect)

    def respond(body, number):
        if number == 1:
            return tool_response([call("hidden", "read_file", path="secret.txt")])
        assert not outputs(body)[-1]["ok"]
        return answer("未读取")

    with application(tmp_path, respond) as (app, _, _):
        assert app.send("read") == "未读取"


def test_preview_during_turn_is_pure_and_next_turn_resets(tmp_path):
    app_ref = []
    seen = []

    def respond(body, number):
        seen.append(names(body))
        if number == 1:
            return tool_response([call("discover", "discover_tools", groups=["files"])])
        if number == 2:
            assert "read_file" in names(body)
            assert [x["function"]["name"] for x in app_ref[0].preview_context().tools] == [
                "list_tools",
                "discover_tools",
            ]
            return tool_response([call("repeat", "discover_tools", names=["read_file"])])
        if number == 3:
            assert outputs(body)[-1]["added_count"] == 0
        return answer()

    with application(tmp_path, respond) as (app, _, _):
        app_ref.append(app)
        app.send("first")
        app.send("second")
    assert seen[1] == seen[2] and seen[3] == seen[0]


def test_invalid_discovery_is_atomic_and_listing_does_not_activate(tmp_path):
    def respond(body, number):
        assert names(body) == ["list_tools", "discover_tools"]
        if number == 1:
            return tool_response([call("invalid", "discover_tools", names=["read_file", "absent"])])
        if number == 2:
            assert not outputs(body)[-1]["ok"]
            return tool_response([call("list", "list_tools", group="files", limit=1)])
        page = outputs(body)[-1]
        assert len(page["tools"]) == 1 and page["next_offset"] == 1
        assert set(page["tools"][0]) == {"name", "group", "summary"}
        return answer()

    with application(tmp_path, respond) as (app, _, _):
        app.send("list")


@pytest.mark.parametrize("limit", ["request", "tool", "input"])
def test_discovery_uses_existing_budgets(tmp_path, limit):
    config = settings(tmp_path)
    if limit == "request":
        config = replace(config, agent=replace(config.agent, max_model_requests=1))
    elif limit == "tool":
        config = replace(config, agent=replace(config.agent, max_tool_calls=1))
    else:
        config = replace(
            config, context=replace(config.context, max_input_tokens=1, safety_margin_tokens=0)
        )
    (tmp_path / "note.txt").write_text("42", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            return tool_response([call("discover", "discover_tools", names=["read_file"])])
        return tool_response([call("read", "read_file", path="note.txt")])

    with application(tmp_path, respond, config=config) as (app, store, bodies):
        with pytest.raises(BudgetError):
            app.send("task")
        assert len(bodies) == {"request": 1, "tool": 2, "input": 0}[limit]
        events = store.load_events(app.session.session_id)
        assert not any(
            e["type"] == "tool_started" and e["payload"]["name"] == "read_file" for e in events
        )


def test_request_log_failure_stops_before_http(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise StorageError("injected request save failure")

    with application(tmp_path, lambda *_: pytest.fail("HTTP must not run")) as (app, store, bodies):
        # 故障注入请求开始记录的持久化边界，发现工具也不能绕过先记录后执行。
        monkeypatch.setattr(store, "start_request", fail)
        with pytest.raises(StorageError):
            app.send("task")
        assert bodies == []


def test_discovered_edit_still_requires_approval(tmp_path):
    import hashlib

    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    approvals = []

    def respond(body, number):
        if number == 1:
            return tool_response([call("discover", "discover_tools", names=["replace_text"])])
        return tool_response(
            [
                call(
                    "edit",
                    "replace_text",
                    path="target.txt",
                    old_text="before",
                    new_text="after",
                    expected_version=hashlib.sha256(b"before").hexdigest(),
                )
            ]
        )

    def reject(plan):
        approvals.append(plan)
        return False

    with application(tmp_path, respond, approve=reject) as (app, _, bodies):
        with pytest.raises(ApprovalDeniedError):
            app.send("edit")
        assert len(bodies) == 2
    assert len(approvals) == 1 and target.read_bytes() == b"before"


def test_plan_directory_omits_mutation_and_full_mode_has_no_discovery_helpers(tmp_path):
    def respond(body, number):
        assert "read_file" in names(body)
        assert not {"replace_text", "run_command", "discover_tools", "list_tools"} & set(
            names(body)
        )
        return answer()

    config = settings(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="full"))
    with application(tmp_path, respond, config=config) as (app, _, _):
        app.select_mode("plan")
        app.send("plan")


def test_dynamic_recording_replays_without_external_actions(tmp_path, monkeypatch):
    from adam.recording.runtime import replay_turn
    from tests.features.test_turn_replay import captured_app, forbid_external_actions

    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")

    def respond(body, number):
        calls = [
            [call("discover", "discover_tools", names=["read_file"])],
            [call("read", "read_file", path="note.txt")],
        ]
        return httpx.Response(
            200, json=tool_response(calls[number - 1]) if number <= 2 else answer()
        )

    with captured_app(tmp_path, respond, config=settings(tmp_path)) as (app, recorder, _, path, _):
        assert recorder.run(lambda: app.send("task")) == "done"
    (tmp_path / "note.txt").unlink()
    forbid_external_actions(monkeypatch)
    result = replay_turn(path, tmp_path / "offline")
    assert result["status"] == "matched"

"""T3.2：显式技能在首个请求生效，列表零请求，快照、预算和失败遵循应用边界。"""

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import CommandResult, handle_command
from adam.config.loader import load_config
from adam.config.settings import SkillsConfig
from adam.contracts.errors import AgentError, BudgetError, ConfigError, StorageError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_standard_skill_metadata import standard
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.test_config import LLM


@contextmanager
def skill_application(workspace, config, handler):
    """保留真实配置、文件与会话，仅替代远端 HTTP；退出释放应用资源。"""
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), workspace)
        try:
            yield app, store
        finally:
            app.close()


def selected_records(payload):
    return [
        json.loads(message["content"].split("\n", 1)[1])
        for message in payload["messages"]
        if isinstance(message.get("content"), str)
        and message["content"].startswith("以下是用户为本轮明确指定的 Skill")
    ]


def no_request(_):
    pytest.fail("this operation must not reach the model")


def test_list_shows_active_and_overridden_sources_without_reading_bodies(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    source, _ = standard(workspace, body=b"PROJECT_BODY")
    standard(user_home, body=b"USER_BODY")
    config = replace(config_at(tmp_path), skills=SkillsConfig(user_dir=user_home / "skills"))
    shown = []
    with skill_application(workspace, config, no_request) as (app, store):
        # 初始化之后删正文仍能查看固定目录；列表不应打开正文。
        source.unlink()
        assert handle_command("/skills", app, shown.append) is CommandResult.HANDLED
        text = "\n".join(shown)
        assert "[项目，生效]" in text and "[用户，被覆盖]" in text
        assert str(source) in text and str(user_home / "skills") in text
        assert "PROJECT_BODY" not in text and "USER_BODY" not in text
        assert app.session is None and store.list_sessions() == []


def test_explicit_skill_is_in_first_request_once_and_does_not_stick_to_next_turn(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    source, header = standard(workspace, body=b"EXPLICIT_PROJECT_BODY")
    standard(user_home, body=b"SHADOWED_USER_BODY")
    (source.parent / "details.md").write_text("UNLOADED_DETAILS", encoding="utf-8")
    (workspace / "evidence.txt").write_text("actual evidence", encoding="utf-8")
    config = replace(config_at(tmp_path), skills=SkillsConfig(user_dir=user_home / "skills"))
    payloads, shown = [], []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        selection = selected_records(payload)
        encoded = json.dumps(payload)
        assert "SHADOWED_USER_BODY" not in encoded and "UNLOADED_DETAILS" not in encoded
        if len(payloads) <= 2:
            assert len(selection) == 1
            assert selection[0]["content"] == "EXPLICIT_PROJECT_BODY"
            assert selection[0]["catalog"]["scope"] == "project"
            assert selection[0]["catalog"]["source"] == str(source)
            assert "details.md" in selection[0]["resources"]
            assert encoded.count("EXPLICIT_PROJECT_BODY") == 1
        else:
            assert not selection and "EXPLICIT_PROJECT_BODY" not in encoded
        if len(payloads) == 1:
            return httpx.Response(
                200, json=tool_response([call("read", "read_file", path="evidence.txt")])
            )
        return httpx.Response(200, json=answer("已核对"))

    with skill_application(workspace, config, handle) as (app, store):
        assert (
            handle_command("/skill inspect-code 核对文件", app, shown.append)
            is CommandResult.HANDLED
        )
        assert app.send("普通下一题") == "已核对"
        sid = app.session.session_id
        events = store.load_events(sid)
        requests = [event["payload"] for event in events if event["type"] == "request_started"]
        assert requests[0]["context"]["selected_skill"] == requests[1]["context"]["selected_skill"]
        assert "selected_skill" not in requests[2]["context"]
        assert requests[0]["context"]["estimated_input_tokens"] > 0
        calls = [e["payload"]["name"] for e in events if e["type"] == "tool_started"]
        assert calls == ["read_file"]
        assert [m.content for m in app.history() if m.role == "user"] == ["核对文件", "普通下一题"]
        saved_selection = requests[0]["context"]["selected_skill"]
    source.write_bytes(header + b"CHANGED_GUIDANCE")
    with skill_application(workspace, config, no_request) as (app, store):
        app.resume(sid)
        assert store.load_events(sid) == events
        assert saved_selection["content"] == "EXPLICIT_PROJECT_BODY"
        assert app.preview_context().selected_skill is None


def test_explicit_body_is_protected_by_budget_before_remote_or_compaction(tmp_path):
    standard(tmp_path, body=b"x" * 2000)
    config = config_at(tmp_path)
    with skill_application(tmp_path, config, no_request) as (app, _):
        base = app.preview_context("检查").estimate.input_tokens
    bounded = replace(
        config,
        context=replace(
            config.context,
            max_input_tokens=base + 101,
            safety_margin_tokens=100,
            auto_compact=True,
        ),
    )
    with skill_application(tmp_path, bounded, no_request) as (app, store):
        with pytest.raises(BudgetError):
            app.send("检查", skill_id="inspect-code")
        events = store.load_events(app.session.session_id)
        assert not any(e["type"] in {"request_started", "compaction_started"} for e in events)
        assert events[-1]["payload"]["reason"] == "context_input_limit"


@pytest.mark.parametrize("failure", ["unknown", "disabled", "missing", "oversized", "io"])
def test_explicit_selection_failure_does_not_create_turn_or_request(tmp_path, monkeypatch, failure):
    source, header = standard(tmp_path)
    config = config_at(tmp_path)
    if failure == "disabled":
        config = replace(config, skills=SkillsConfig(enabled=False))
    with skill_application(tmp_path, config, no_request) as (app, store):
        if failure == "missing":
            source.unlink()
        elif failure == "oversized":
            source.write_bytes(header + b"x" * 9000)
        elif failure == "io":
            original_open = Path.open

            def unavailable(path, *args, **kwargs):
                if path == source:
                    raise OSError("simulated source read failure")
                return original_open(path, *args, **kwargs)

            monkeypatch.setattr(Path, "open", unavailable)
        with pytest.raises(AgentError):
            app.send("检查", skill_id="unknown" if failure == "unknown" else "inspect-code")
        assert app.session is None and store.list_sessions() == []


def test_failed_selection_does_not_present_previous_execution_as_current(tmp_path):
    standard(tmp_path)
    with skill_application(
        tmp_path, config_at(tmp_path), lambda _: httpx.Response(200, json=answer("previous"))
    ) as (app, store):
        app.send("上一轮")
        sid = app.session.session_id
        original_events = store.load_events(sid)
        assert app.execution_report() is not None
        with pytest.raises(AgentError):
            app.send("新任务", skill_id="missing")
        assert app.execution_report() is None
        assert store.load_events(sid) == original_events


@pytest.mark.parametrize("command", ["/skill", "/skill inspect-code", "/skills extra"])
def test_invalid_skill_command_syntax_is_handled_without_sending(tmp_path, command):
    standard(tmp_path)
    shown = []
    with skill_application(tmp_path, config_at(tmp_path), no_request) as (app, _):
        assert handle_command(command, app, shown.append) is CommandResult.HANDLED
        assert shown[0].startswith("用法：")
        assert app.session is None


def test_user_skill_directory_resolves_beside_config_and_keeps_permissions_separate(tmp_path):
    directory = tmp_path / "settings"
    directory.mkdir()
    path = directory / "config.toml"
    (directory / "llm.toml").write_text(LLM, encoding="utf-8")
    path.write_text("", encoding="utf-8")
    assert load_config(path).skills.user_dir == directory / "skills"
    source, _ = standard(tmp_path / "custom")
    path.write_text('[skills]\nuser_dir="../custom/skills"', encoding="utf-8")
    config = load_config(path)
    assert config.skills.user_dir == directory / "../custom/skills"
    workspace = tmp_path / "project"
    workspace.mkdir()
    with skill_application(workspace, config, no_request) as (app, _):
        assert app.list_skills()[0].source == str(source)
    assert config.permissions.additional_read_dirs == ()
    absolute = tmp_path / "external"
    path.write_text(f'[skills]\nuser_dir="{absolute.as_posix()}"', encoding="utf-8")
    assert load_config(path).skills.user_dir == absolute


@pytest.mark.parametrize("value", ['""', "true", "123", "[]"])
def test_invalid_user_skill_directory_reports_config_error(tmp_path, value):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(f"[skills]\nuser_dir={value}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("changed", ["content", "source", "resource"])
def test_selected_skill_snapshot_rejects_corruption_without_rewriting_log(tmp_path, changed):
    standard(tmp_path, body=b"selected original")
    config = config_at(tmp_path)
    with skill_application(
        tmp_path, config, lambda _: httpx.Response(200, json=answer("done"))
    ) as (app, store):
        app.send("检查", skill_id="inspect-code")
        sid, events = app.session.session_id, store.load_events(app.session.session_id)
    record = next(e for e in events if e["type"] == "request_started")
    selection = record["payload"]["context"]["selected_skill"]
    if changed == "content":
        selection["content"] = "altered without matching hash"
    elif changed == "source":
        selection["catalog"]["source"] = str(tmp_path / "forged" / "SKILL.md")
    else:
        selection["resources"] = ["../outside.md"]
    path = next(config.sessions_dir.glob(f"*/*/*/{sid}.jsonl"))
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
    )
    original = path.read_bytes()
    with SessionStore(config.sessions_dir) as store:
        with pytest.raises(StorageError):
            store.select_session(sid)
    assert path.read_bytes() == original

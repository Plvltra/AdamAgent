"""E1.2：真实 CLI 命令经应用接口管理记忆，不发送模型请求。"""

import os

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import CommandResult, handle_command
from adam.config.settings import AppConfig, LLMConfig
from adam.contracts.errors import AgentError, MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore


@pytest.fixture
def memory_app(tmp_path):
    def reject_http(request):
        pytest.fail("memory management must not call the model")

    config = AppConfig(
        LLMConfig("https://example.test", "test", "unused", stream=False),
        tmp_path / "sessions",
        "system",
        memories_dir=tmp_path / "memory",
    )
    with (
        httpx.Client(transport=httpx.MockTransport(reject_http)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        try:
            yield app
        finally:
            app.close()


def test_cli_memory_lifecycle_preserves_content_and_sources(memory_app):
    app = memory_app
    shown = []
    content = '代码目录 C:\\work\\my project，保持 "原文" -- 不执行'
    assert handle_command("/memory add -- " + content, app, shown.append) is CommandResult.HANDLED
    first = app.manage_memory(MemoryCommand("list"))[0]
    assert first.content == content
    assert first.source.session_id is None
    assert app.session is None and app.list_sessions() == []
    session = app.new("source")
    handle_command(
        f"/memory update {first.memory_id} --revision 1 --expires 2020-01-01T00:00:00Z -- 新约定",
        app,
        shown.append,
    )
    assert "已过期" in "\n".join(shown)
    changed = app.manage_memory(MemoryCommand("list"))[0]
    assert changed.revision == 2 and changed.source.session_id == session.session_id
    with pytest.raises(MemoryInputError, match="已被其他"):
        handle_command(f"/memory delete {first.memory_id} --revision 1", app, shown.append)
    handle_command(f"/memory delete {first.memory_id} --revision 2", app, shown.append)
    assert app.manage_memory(MemoryCommand("list")) == []
    handle_command(f"/memory list --history {first.memory_id}", app, shown.append)
    assert "已删除" in "\n".join(shown)
    assert (
        len(app.manage_memory(MemoryCommand("list", memory_id=first.memory_id, history=True))) == 3
    )


def test_global_mutation_requires_explicit_scope_and_update_can_clear_expiry(memory_app):
    app = memory_app
    shown = []
    handle_command(
        "/memory add --global --expires 2020-01-01T00:00:00Z -- 回答使用中文",
        app,
        shown.append,
    )
    record = app.manage_memory(MemoryCommand("list", global_scope=True))[0]
    with pytest.raises(MemoryInputError, match="没有"):
        handle_command(f"/memory delete {record.memory_id} --revision 1", app, shown.append)
    handle_command(
        f"/memory update {record.memory_id} --revision 1 --global -- 回答使用简体中文",
        app,
        shown.append,
    )
    assert app.manage_memory(MemoryCommand("list", global_scope=True))[0].expires_at is None


@pytest.mark.parametrize(
    "command",
    [
        "/memory unknown",
        "/memory add",
        "/memory add stray -- body",
        "/memory add --bad -- body",
        "/memory update 1234abcd -- new",
        "/memory delete 1234abcd --revision no",
        "/memory list -- body",
        "/memory list --expires 2030-01-01",
        "/memory add --expires 2030-01-01 -- body",
        "/memory add --history 1234abcd -- body",
    ],
)
def test_invalid_commands_do_not_save_or_exit_terminal(memory_app, command):
    with pytest.raises(AgentError):
        handle_command(command, memory_app, lambda _: None)
    assert memory_app.manage_memory(MemoryCommand("list")) == []
    assert memory_app.session is None


def test_cli_write_failure_never_claims_success(memory_app, monkeypatch):
    shown = []

    def fail(*args):
        raise PermissionError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(MemoryStoreError):
        handle_command("/memory add -- not saved", memory_app, shown.append)
    assert not shown
    assert memory_app.manage_memory(MemoryCommand("list")) == []

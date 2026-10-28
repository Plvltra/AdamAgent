"""应用边界：替换模型、扩展工具、保留失败信息，以及资源退出后的可用性。"""

import json

import httpx
import pytest

import adam.bootstrap as bootstrap
from adam.application import AgentApplication
from adam.bootstrap import build_application, open_application
from adam.cli import chat
from adam.conversation import Conversation
from adam.errors import AgentError, CommandLogError, LLMError, StorageError, TurnSaveError
from adam.models import Completion
from adam.settings import AgentConfig, AppConfig, LLMConfig, ToolLimits
from adam.storage import SessionStore
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.registry import ToolRegistry


class MemoryModel:
    """无 config 或 HTTP 属性的模型替身，用于验证业务只需要模型接口。"""

    def __init__(self, completions):
        self.completions = iter(completions)
        self.requests = []

    def list_models(self):
        return {"first": "First", "second": "Second"}

    def complete(self, messages, *, model, tools=None, max_tokens=4096, on_output=None):
        self.requests.append((list(messages), model))
        result = next(self.completions)
        if isinstance(result, Exception):
            raise result
        return result


def test_cli_accepts_application_without_model_configuration(tmp_path):
    """完整 CLI 通过应用切换模型与查询用量，不读取模型客户端的 config。"""
    model = MemoryModel(
        [
            Completion({"role": "assistant", "content": "one"}, (2, 1)),
            Completion({"role": "assistant", "content": "two"}, (3, 1)),
        ]
    )
    config = AppConfig(
        LLMConfig("https://unused.test", "first", "secret", stream=False), tmp_path, "system"
    )
    inputs = iter(["hello", "/model second", "again", "/usage", "/exit"])
    output = []
    with SessionStore(tmp_path / "sessions") as store:
        application = build_application(config, store, model, tmp_path)
        chat(application, read=lambda _: next(inputs), write=output.append)
        assert [request[1] for request in model.requests] == ["first", "second"]
        assert len(application.history()) == 4
        assert application.usage().tokens == 7
        assert any("7/50000" in line for line in output)
        assert config.llm.model == "first"
        # 交互循环退出不关闭外部拥有的应用，仍可继续读取或执行应用操作。
        application.new("after CLI")
        assert len(application.list_sessions()) == 2


def test_custom_tool_uses_existing_loop_and_records_trace(tmp_path):
    """注册新工具即可经过原有执行和留痕流程，核心循环不识别该工具名称。"""
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "one",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text":"evidence"}'},
            }
        ],
    }
    model = MemoryModel(
        [
            Completion(tool_message, (2, 1)),
            Completion({"role": "assistant", "content": "done"}, (2, 1)),
        ]
    )
    specs = {
        "echo": {
            "description": "Echo text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    }
    registry = ToolRegistry(
        specs, {"echo": lambda args, record: {"text": args["text"]}}, ToolLimits()
    )
    events = []
    with SessionStore(tmp_path / "sessions") as store:
        conversation = Conversation(
            store,
            model,
            "",
            ToolExecutor(store, registry, events.append),
            AgentConfig(),
            model="first",
        )
        assert conversation.send("echo") == "done"
        output = json.loads(model.requests[-1][0][-1]["content"])
        assert output == {"ok": True, "text": "evidence"}
        assert events == ["echo"]
        session_id = conversation.session.session_id
        records = store.load_events(session_id)
        finished = [entry for entry in records if entry["type"] == "tool_finished"]
        assert len(finished) == 1
        assert json.loads(finished[0]["payload"]["output"]) == output


@pytest.mark.parametrize(
    "error,expected,reply",
    [
        (LLMError("请求超时", (1, 2)), "模型请求错误：请求超时", None),
        (CommandLogError("执行日志保存失败"), "执行日志保存失败", None),
        (StorageError("会话文件操作失败"), "会话文件操作失败", None),
        (TurnSaveError("already answered"), "未加入历史", "already answered"),
    ],
)
def test_application_exposes_safe_error_without_losing_reply(tmp_path, error, expected, reply):
    """在应用依赖的 send 边界注入业务故障；公共错误、原因链和未保存回答原样传递。"""
    cause = OSError("private system details")
    error.__cause__ = cause

    class FailingConversation:
        def send(self, text):
            raise error

    with SessionStore(tmp_path / "sessions") as store:
        application = AgentApplication(FailingConversation(), store, MemoryModel([]))
        with pytest.raises(AgentError) as caught:
            application.send("test")
    assert expected in str(caught.value)
    assert "private system details" not in str(caught.value)
    assert caught.value.partial_reply == reply
    assert caught.value is error
    assert caught.value.__cause__ is cause


@pytest.mark.parametrize("failure", ["none", "workspace", "resume", "interface"])
def test_owned_resources_close_on_exit_or_failure(tmp_path, monkeypatch, failure):
    """真实打开配置、HTTP 与存储；正常退出、组装失败、恢复失败或界面异常都释放资源。"""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(
        '[llm]\nstream=false\nbase_url="https://example.test"\nmodel="first"\napi_key="secret"\n',
        encoding="utf-8",
    )
    http = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP expected")))
    monkeypatch.setattr(bootstrap.httpx, "Client", lambda **kwargs: http)
    closed = []
    original_close = SessionStore.close

    def close(store):
        closed.append(store)
        original_close(store)

    monkeypatch.setattr(SessionStore, "close", close)
    workspace = tmp_path / "missing" if failure == "workspace" else tmp_path
    session_id = None
    try:
        with open_application(config_path, None, workspace) as application:
            if failure == "resume":
                application.resume("deadbeef")
            else:
                session_id = application.new("held lock").session_id
            if failure == "interface":
                raise RuntimeError("UI failed")
    except (AgentError, RuntimeError):
        assert failure != "none"
    else:
        assert failure == "none"
    assert http.is_closed
    assert closed
    if session_id:
        with SessionStore(tmp_path / "sessions") as reopened:
            assert reopened.select_session(session_id).title == "held lock"

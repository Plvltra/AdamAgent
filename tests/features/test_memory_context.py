"""E1.3：跨会话检索检查实际 HTTP 输入与请求记录，不依赖模型口头承诺。"""

import json

import httpx

from adam.bootstrap import build_application
from adam.cli.main import handle_command
from adam.config.settings import AppConfig, ContextConfig, LLMConfig, MemoryConfig
from adam.contracts.memory import MemoryCommand
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore


def config_at(root, *, memories_dir=None, memory=None):
    """所有数据留在测试目录，多个应用只显式共享记忆库。"""
    return AppConfig(
        LLMConfig("https://example.test", "test", "unused", stream=False),
        root / "sessions",
        "system constraint",
        memories_dir=memories_dir or root / "memory",
        memory=memory or MemoryConfig(),
        context=ContextConfig(auto_compact=False),
    )


def completion(tool=False):
    message = {"role": "assistant", "content": "answer"}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                }
            ],
        }
    return {
        "choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def memory_records(payload):
    """只解析程序注入的资料消息，避免把助手普通回答误认为记忆选择。"""
    return [
        record
        for message in payload["messages"]
        if isinstance(message.get("content"), str)
        and message["content"].startswith("以下是用户明确保存")
        for record in json.loads(message["content"].split("\n", 1)[1])
    ]


def test_new_session_recall_update_delete_and_durable_request_evidence(tmp_path):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    config = config_at(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        source = app.new("A")
        item = app.manage_memory(MemoryCommand("add", "Python 使用绝对导入"))[0]
        app.manage_memory(MemoryCommand("add", "午餐喜欢面条"))
        target = app.new("B")
        app.send("Python 导入约定是什么")
        assert memory_records(payloads[-1]) == [item.to_dict()]
        assert item.source.session_id == source.session_id
        assert all("绝对导入" not in message.content for message in app.history())
        event = next(
            e for e in store.load_events(target.session_id) if e["type"] == "request_started"
        )
        assert event["payload"]["context"]["memory"]["records"] == [item.to_dict()]
        updated = app.manage_memory(
            MemoryCommand("update", "Python 导入需要明确模块路径", item.memory_id, 1)
        )[0]
        app.send("Python 导入约定是什么")
        assert memory_records(payloads[-1]) == [updated.to_dict()]
        app.manage_memory(MemoryCommand("delete", memory_id=item.memory_id, revision=2))
        app.send("Python 导入约定是什么")
        assert memory_records(payloads[-1]) == []
        assert event["payload"]["context"]["memory"]["records"][0]["content"] == item.content
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.resume(target.session_id)
        assert (
            next(e for e in store.load_events(target.session_id) if e["type"] == "request_started")
            == event
        )
        app.send("Python 导入约定是什么")
        assert not memory_records(payloads[-1])


def test_project_scope_global_scope_expiry_and_unrelated_queries(tmp_path):
    project_a, project_b = tmp_path / "A", tmp_path / "B"
    project_a.mkdir()
    project_b.mkdir()
    shared = tmp_path / "shared"
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    for root in (project_a, project_b):
        config = config_at(root, memories_dir=shared)
        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as http,
            SessionStore(config.sessions_dir) as store,
        ):
            app = build_application(config, store, LLMClient(config.llm, http), root)
            if root == project_a:
                private = app.manage_memory(MemoryCommand("add", "Python alpha 私有约定"))[0]
                common = app.manage_memory(
                    MemoryCommand("add", "Python 公共约定", global_scope=True)
                )[0]
                app.manage_memory(
                    MemoryCommand(
                        "add", "Python 已过期", global_scope=True, expires_at="2020-01-01T00:00:00Z"
                    )
                )
            else:
                app.send("Python alpha 约定")
                assert memory_records(payloads[-1]) == [common.to_dict()]
                assert private.memory_id not in json.dumps(payloads[-1])
                app.send("午餐吃什么")
                assert memory_records(payloads[-1]) == []


def test_tool_loop_pins_memory_and_next_turn_refreshes(tmp_path):
    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")
    payloads = []
    app = None
    item = None

    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            app.manage_memory(MemoryCommand("update", "Python 新约定", item.memory_id, 1))
        return httpx.Response(200, json=completion(tool=len(payloads) == 1))

    config = config_at(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        item = app.manage_memory(MemoryCommand("add", "Python 原约定"))[0]
        app.send("Python 约定，读取文件")
        assert memory_records(payloads[0]) == memory_records(payloads[1]) == [item.to_dict()]
        app.send("Python 约定")
        assert memory_records(payloads[2])[0]["revision"] == 2


def test_query_preview_has_no_model_or_session_side_effect(tmp_path):
    def handler(request):
        raise AssertionError("preview must not request a model")

    config = config_at(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        item = app.manage_memory(MemoryCommand("add", "Python 绝对导入"))[0]
        shown = []
        handle_command("/context full --query Python 导入", app, shown.append)
        assert any(item.memory_id in line for line in shown)
        assert any("假设预览" in line for line in shown)
        assert app.preview_context().memory.status == "query_required"
        assert store.list_sessions() == []


def test_memory_budget_does_not_evict_protected_input(tmp_path):
    config = config_at(tmp_path, memory=MemoryConfig(max_input_tokens=1))
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.manage_memory(MemoryCommand("add", "Python 绝对导入"))
        preview = app.preview_context("Python 导入")
        assert preview.memory.status == "budget"
        assert preview.estimate.memory_tokens == 0
        app.send("Python 导入")
        assert memory_records(payloads[-1]) == []
        assert payloads[-1]["messages"][0]["content"] == "system constraint"
        assert payloads[-1]["messages"][-1]["content"] == "Python 导入"

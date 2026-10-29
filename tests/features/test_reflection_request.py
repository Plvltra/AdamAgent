"""E4.2：真实请求适配与 JSONL，专门反思不执行工具，格式和记账失败分开。"""

import json

import httpx
import pytest

from adam.application.reflection import ReflectionService
from adam.application.request_policy import RequestPolicy
from adam.application.request_runner import RequestRunner
from adam.config.settings import ContextConfig, LLMConfig, ReflectionConfig
from adam.contracts.errors import LLMError, StorageError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_reflection_feedback import feedback_turn
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response

SUGGESTION = {
    "problem": "文件不存在",
    "hypothesis": "路径基准可能不对",
    "action": "检查配置目录与工作目录",
    "verification": "读取正确路径并运行项目检查",
    "lesson": "读取配置中的相对路径前先确认基准目录",
    "applies_when": "处理配置相对路径时",
    "limits": "经验尚未验证，不能推广到所有路径",
}


@pytest.mark.parametrize("mode", ["valid", "bad_json", "tool_call", "bad_protocol"])
def test_reflection_response_is_recorded_separately_and_never_executes_tools(tmp_path, mode):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert "tools" not in body and body["max_tokens"] == 1024
        assert "missing file" in body["messages"][-1]["content"]
        data = answer(json.dumps(SUGGESTION))
        if mode == "bad_json":
            data = answer("not json")
        elif mode == "tool_call":
            data = tool_response([call("forged", "run_command", argv=["must-not-run"])])
        elif mode == "bad_protocol":
            data["choices"] = []
        return httpx.Response(200, json=data)

    with (
        SessionStore(tmp_path) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        session_id, feedback = feedback_turn(store)
        store.append_feedback(session_id, feedback)
        client = LLMClient(LLMConfig("https://unused.test", "test", "secret", stream=False), http)
        policy = RequestPolicy(8, lambda _: None, used=1)
        service = ReflectionService(store, ReflectionConfig(), ContextConfig())
        result = service.reflect(
            session_id, feedback, "test", RequestRunner(store, client, lambda _: None), policy
        )
        assert result["status"] == ("completed" if mode == "valid" else "invalid")
        assert result["result"] == (SUGGESTION if mode == "valid" else None)
        assert len(bodies) == 1 and policy.used == 2
        assert store.usage(session_id) == (24, False)
        assert len(store.load_messages(session_id)) == 2
        assert len(store.load_reflections(session_id)) == 1
        assert store.load_executions(session_id) == []
        events = store.load_events(session_id)
        assert [e["payload"]["tool_call_id"] for e in events if e["type"] == "tool_started"] == [
            "work"
        ]


@pytest.mark.parametrize("boundary", ["unknown_usage", "settlement", "reflection_record"])
def test_reflection_failure_does_not_hide_unknown_usage_or_storage_error(
    tmp_path, monkeypatch, boundary
):
    def handler(request):
        data = answer(json.dumps(SUGGESTION))
        if boundary == "unknown_usage":
            data.pop("usage")
        return httpx.Response(200, json=data)

    def fail(*args, **kwargs):
        raise StorageError("injected save failure")

    with (
        SessionStore(tmp_path) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        session_id, feedback = feedback_turn(store)
        store.append_feedback(session_id, feedback)
        if boundary != "unknown_usage":
            monkeypatch.setattr(
                store, "finish_request" if boundary == "settlement" else "append_reflection", fail
            )
        client = LLMClient(LLMConfig("https://unused.test", "test", "secret", stream=False), http)
        service = ReflectionService(store, ReflectionConfig(), ContextConfig())
        with pytest.raises(LLMError if boundary == "unknown_usage" else StorageError):
            service.reflect(
                session_id,
                feedback,
                "test",
                RequestRunner(store, client, lambda _: None),
                RequestPolicy(8, lambda _: None, used=1),
            )
        assert store.load_reflections(session_id) == []
        assert store.usage(session_id)[1] == (boundary != "reflection_record")


@pytest.mark.parametrize(
    "input_limit,requests,reason", [(4096, 2, "request_budget"), (1, 8, "input_budget")]
)
def test_reflection_skips_without_spending_last_answer_request(
    tmp_path, input_limit, requests, reason
):
    def handler(request):
        pytest.fail("skipped reflection must not call HTTP")

    with (
        SessionStore(tmp_path) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        session_id, feedback = feedback_turn(store)
        store.append_feedback(session_id, feedback)
        client = LLMClient(LLMConfig("https://unused.test", "test", "secret", stream=False), http)
        service = ReflectionService(
            store, ReflectionConfig(max_input_tokens=input_limit), ContextConfig()
        )
        policy = RequestPolicy(requests, lambda _: None, used=1)
        result = service.reflect(
            session_id, feedback, "test", RequestRunner(store, client, lambda _: None), policy
        )
        assert result["status"] == "skipped" and result["reason"] == reason
        assert policy.used == 1 and store.usage(session_id) == (12, False)

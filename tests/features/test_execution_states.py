"""验证请求和轮次独立收尾；持久化故障不能掩盖原始失败。"""

import json

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import LLMError, StorageError
from adam.llm.client import LLMClient
from adam.session.log import parse_log
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def snapshot(store, session_id):
    """从实际 JSONL 重新构建状态，不使用聊天对象中的临时变量。"""
    path = next(store.root.glob(f"*/*/*/{session_id}.jsonl"))
    return parse_log(path.read_bytes(), session_id)


def make_conversation(store, http, workspace, notify=None):
    return build_conversation(
        store,
        LLMClient(LLMConfig("https://example.test", "test", "secret", stream=False), http),
        "",
        create_tool_registry(workspace),
        notify=notify,
    )


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, TypeError])
@pytest.mark.parametrize("phase", ["request", "tool"])
def test_failure_closes_only_operations_still_running(tmp_path, failure_type, phase):
    """请求内中断与请求成功后的工具阶段中断，具有不同的请求终态。"""
    failure = failure_type("private-detail")
    requests = []
    root = tmp_path / "sessions"

    def handle(request):
        requests.append(request)
        log = snapshot(store, conversation.session.session_id)
        assert [t["status"] for t in log.turns.values()] == ["in_progress"]
        assert [r["status"] for r in log.requests.values()] == ["in_progress"]
        if phase == "request":
            raise failure
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "file1",
                                    "type": "function",
                                    "function": {"name": "file_info", "arguments": '{"path":"."}'},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    def notify(message):
        raise failure

    with httpx.Client(transport=httpx.MockTransport(handle)) as http, SessionStore(root) as store:
        conversation = make_conversation(store, http, tmp_path, notify)
        with pytest.raises(failure_type) as caught:
            conversation.send("inspect")
        assert caught.value is failure
        session_id = conversation.session.session_id
    with SessionStore(root) as store:
        log = snapshot(store, session_id)
        status = "failed" if failure_type is TypeError else "interrupted"
        assert [t["status"] for t in log.turns.values()] == [status]
        assert [r["status"] for r in log.requests.values()] == [
            status if phase == "request" else "succeeded"
        ]
        assert store.usage(session_id) == ((0, True) if phase == "request" else (12, False))
        assert store.load_messages(session_id) == []
        assert "private-detail" not in json.dumps(store.load_events(session_id))
    assert len(requests) == 1


@pytest.mark.parametrize("has_usage", [False, True])
def test_valid_response_success_is_independent_of_accounting(tmp_path, has_usage):
    """有效回答没有用量时请求仍成功，但整轮按既有预算规则失败。"""
    result = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]
    }
    if has_usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result))
        ) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = make_conversation(store, http, tmp_path)
        if has_usage:
            assert conversation.send("question") == "done"
        else:
            with pytest.raises(LLMError, match="可靠用量"):
                conversation.send("question")
        session_id = conversation.session.session_id
        log = snapshot(store, session_id)
        assert [r["status"] for r in log.requests.values()] == ["succeeded"]
        assert [t["status"] for t in log.turns.values()] == ["succeeded" if has_usage else "failed"]
        assert store.usage(session_id) == ((12, False) if has_usage else (0, True))
        assert len(store.load_messages(session_id)) == (2 if has_usage else 0)
        end = store.load_events(session_id)[-1]["payload"]
        assert end["reason"] == ("completed" if has_usage else "usage_unknown")
        assert end["duration_ms"] >= 0


@pytest.mark.parametrize("failing_method", ["finish_request", "finish_turn"])
def test_journal_failure_preserves_original_api_error(tmp_path, monkeypatch, failing_method):
    """模拟错误收尾本身无法落盘，原始 API 错误与未知用量仍保留，不重试。"""
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503, text="private-server-detail")

    def fail(*args, **kwargs):
        raise StorageError("private-disk-detail")

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        monkeypatch.setattr(store, failing_method, fail)
        conversation = make_conversation(store, http, tmp_path)
        with pytest.raises(LLMError, match="503") as caught:
            conversation.send("question")
        assert any("状态保存失败" in note for note in caught.value.__notes__)
        log = snapshot(store, conversation.session.session_id)
        assert [r["status"] for r in log.requests.values()] == [
            "in_progress" if failing_method == "finish_request" else "failed"
        ]
        assert [t["status"] for t in log.turns.values()] == [
            "in_progress" if failing_method == "finish_turn" else "failed"
        ]
        assert log.unknown_usage and log.messages == []
        assert "private" not in json.dumps(log.events)
    assert len(requests) == 1

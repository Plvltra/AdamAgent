"""手动摘要业务：假 HTTP、真实 JSONL，验证成本、提交边界、来源与恢复。"""

import json

import httpx
import pytest

from adam.application import AgentApplication
from adam.cli import handle_command
from adam.config import AgentConfig, ContextConfig, LLMConfig
from adam.diagnostics import read_report
from adam.errors import AgentError, BudgetError
from adam.llm import LLMClient
from adam.storage import SessionStore, StorageError
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def reply(text="目标：保留代号 ORION；待办：检查文件"):
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }


@pytest.fixture
def chat(tmp_path):
    requests, responses, shown = [], [], []

    def respond(request):
        requests.append(json.loads(request.content))
        response = responses.pop(0) if responses else reply()
        if isinstance(response, BaseException):
            raise response
        return httpx.Response(200, json=response)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        conversation = build_conversation(
            store,
            LLMClient(
                LLMConfig("https://example.test", "model-a", "private-key", stream=False), http
            ),
            "保留项目约束",
            create_tool_registry(tmp_path),
            notify=shown.append,
        )
        yield conversation, requests, responses, shown


def seed(conversation, count=5, text="ORION"):
    session_id = conversation.new().session_id
    for index in range(count):
        conversation.store.append_turn(session_id, f"question {index}", text)
    return session_id


def test_compact_command_preserves_raw_history_and_resumes_summary(chat, tmp_path):
    conversation, requests, _, shown = chat
    assert conversation.compact() is None
    assert conversation.session is None and requests == []
    session_id = seed(conversation)
    store = conversation.store
    original = store.load_messages(session_id)
    conversation.model = "model-b"
    handle_command(
        "/compact extra",
        AgentApplication(conversation, conversation.store, conversation.client),
        shown.append,
    )
    assert requests == []
    handle_command(
        "/compact",
        AgentApplication(conversation, conversation.store, conversation.client),
        shown.append,
    )
    summary = store.load_summary(session_id)
    assert len(summary.covered_turn_ids) == 3
    assert store.load_messages(session_id) == original
    assert requests[0]["model"] == "model-b"
    assert "tools" not in requests[0] and requests[0]["max_tokens"] == 2048
    material = json.loads(requests[0]["messages"][1]["content"])
    assert material["session_constraints"] == "保留项目约束"
    assert len(material["turns"]) == 3 and "ORION" in requests[0]["messages"][1]["content"]
    assert store.usage(session_id) == (30, False)
    path = next(store.root.rglob("*.jsonl"))
    report = read_report(path)
    assert len(report["turns"]) == 5  # 独立摘要不伪造成用户聊天。
    assert report["compactions"][0]["status"] == "succeeded"
    assert report["compactions"][0]["request"]["status"] == "succeeded"
    before = path.read_bytes()
    context = conversation.preview_context()
    assert context.summary == summary
    assert len(context.selected_turn_ids) == 2
    assert context.messages[1] == summary.message()
    assert path.read_bytes() == before
    conversation.compact()  # 最近两轮受保护，没有新素材，不重复请求。
    assert len(requests) == 1
    store.close()
    conversation.resume(session_id)
    assert conversation.preview_context() == context
    conversation.send("next")
    assert requests[-1]["messages"] == context.messages + [{"role": "user", "content": "next"}]
    assert store.usage(session_id) == (60, False)


def test_oversized_history_is_compacted_in_contiguous_batches(chat):
    conversation, requests, _, _ = chat
    seed(conversation, text="中" * 900)
    conversation.context_builder.limits = ContextConfig(
        max_input_tokens=1800, safety_margin_tokens=0
    )
    covered = []
    for expected in (1, 2, 3):
        summary = conversation.compact()
        assert len(summary.covered_turn_ids) == expected
        assert list(summary.covered_turn_ids[: len(covered)]) == covered
        material = json.loads(requests[-1]["messages"][1]["content"])
        assert len(material["turns"]) == 1
        assert (material["previous_summary"] is not None) == (expected > 1)
        covered = list(summary.covered_turn_ids)
    assert conversation.compact() is None and len(requests) == 3


@pytest.mark.parametrize(
    "fault", ["timeout", "invalid", "unknown_usage", "save", "settlement", "interrupt"]
)
def test_failed_summary_keeps_previous_version_and_accounts_for_cost(chat, monkeypatch, fault):
    conversation, requests, responses, _ = chat
    session_id = seed(conversation)
    previous = conversation.compact()
    for _ in range(2):
        conversation.store.append_turn(session_id, "new", "facts")
    if fault == "timeout":
        responses.append(httpx.ReadTimeout("secret remote details"))
    elif fault == "invalid":
        responses.append(reply(" "))
    elif fault == "unknown_usage":
        response = reply()
        response.pop("usage")
        responses.append(response)
    elif fault == "interrupt":
        responses.append(KeyboardInterrupt())
    else:
        original = conversation.store.append_compaction_event
        kind_to_fail = "compaction_completed" if fault == "save" else "compaction_usage"

        def fail_boundary(session_id, kind, payload):
            # 在公开持久化边界模拟失败；操作收尾仍使用实际存储。
            if kind == kind_to_fail:
                raise StorageError("injected save failure")
            return original(session_id, kind, payload)

        monkeypatch.setattr(conversation.store, "append_compaction_event", fail_boundary)
    with pytest.raises((AgentError, KeyboardInterrupt)):
        conversation.compact()
    assert conversation.store.load_summary(session_id) == previous
    assert len(requests) == 2
    tokens, unknown = conversation.store.usage(session_id)
    expected_unknown = fault in {"timeout", "unknown_usage", "settlement", "interrupt"}
    assert unknown == expected_unknown
    assert tokens == (30 if expected_unknown else 60)
    operation = read_report(next(conversation.store.root.rglob("*.jsonl")))["compactions"][-1]
    assert operation["status"] == ("interrupted" if fault == "interrupt" else "failed")
    if expected_unknown:
        with pytest.raises(BudgetError):
            conversation.send("must not spend")
        assert len(requests) == 2


def test_no_source_no_budget_and_huge_source_make_no_request(chat):
    conversation, requests, _, _ = chat
    session_id = seed(conversation, count=2)
    assert conversation.compact() is None and requests == []
    conversation.store.append_turn(session_id, "another", "ok")
    conversation.limits = AgentConfig(max_session_tokens=1)
    conversation.compact()
    with pytest.raises(BudgetError):
        conversation.send("budget gone")
    assert len(requests) == 1
    seed(conversation, count=3, text="中" * 50000)
    with pytest.raises(BudgetError, match="最早待摘要"):
        conversation.compact()
    assert len(requests) == 1


def test_protected_recent_turn_cannot_be_silently_dropped(chat):
    conversation, requests, _, _ = chat
    seed(conversation, count=1, text="中" * 40000)
    with pytest.raises(BudgetError):
        conversation.send("next")
    assert requests == []


def test_summary_source_validation_preserves_log(chat):
    conversation, _, _, _ = chat
    session_id = seed(conversation)
    path = next(conversation.store.root.rglob("*.jsonl"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        conversation.store.append_compaction_event(
            session_id,
            "compaction_started",
            {
                "operation_id": "bad",
                "source_turn_ids": ["missing"],
                "previous_summary_id": None,
                "model": "test",
                "max_output_tokens": 10,
                "estimated_input_tokens": 10,
            },
        )
    assert path.read_bytes() == before

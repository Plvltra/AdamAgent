"""B05.1：摘要参考实际问题和已执行工具，来源与失败边界通过真实 JSONL 核对。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import ContextConfig, LLMConfig
from adam.context.compaction import CompactionService
from adam.contracts.errors import BudgetError, LLMError
from adam.contracts.history import COMPACTION_VERSION
from adam.llm.client import LLMClient
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.support import build_conversation


@pytest.mark.parametrize("failure", [False, True])
def test_compaction_after_real_tool_uses_current_question_chain_and_keeps_originals(
    tmp_path, failure
):
    bodies = []
    (tmp_path / "evidence.txt").write_text("证" * 2000, encoding="utf-8")

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert "tools" in body
            value = tool_response([call("read", "read_file", path="evidence.txt")])
        elif len(bodies) == 2:
            assert "tools" not in body
            material = json.loads(body["messages"][1]["content"])
            task = material["current_task"]
            assert [m["role"] for m in task["messages"]] == ["user", "assistant", "tool"]
            assert task["messages"][0]["content"] == "解释旧决策，先读证据"
            assert task["messages"][2]["tool_call_id"] == "read"
            assert "证" * 1000 in task["messages"][2]["content"]
            assert task["turn_id"] not in [t["turn_id"] for t in material["turns"]]
            value = answer(" " if failure else "保留旧约束，当前读取仅供任务定位。")
        else:
            assert len(bodies) == 3 and "tools" in body
            value = answer("完成解释")
        return httpx.Response(200, json=value)

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            "不要遗漏否定约束",
            create_tool_registry(tmp_path),
            context_config=ContextConfig(max_input_tokens=9000, safety_margin_tokens=0),
        )
        session_id = conversation.new().session_id
        for index in range(6):
            store.append_turn(session_id, f"旧问题 {index}", "旧" * 200)
        original = store.load_turns(session_id)
        # 阈值置于首次请求和真实读文件后的压力之间，确保摘要发生于工具循环中。
        first = conversation.preview_context("解释旧决策，先读证据")
        ratio = (first.candidate_input_tokens + 300) / 9000
        conversation.context_builder.limits = replace(
            conversation.context_builder.limits,
            compression_trigger_ratio=ratio,
            compression_target_ratio=ratio / 2,
        )
        if failure:
            with pytest.raises(LLMError):
                conversation.send("解释旧决策，先读证据")
        else:
            assert conversation.send("解释旧决策，先读证据") == "完成解释"
        assert store.load_turns(session_id)[:6] == original
        summary = store.load_summary(session_id)
        path = next(store.root.rglob(f"{session_id}.jsonl"))
        report = read_report(path)
        operation = report["compactions"][0]
        task = operation["current_task"]
        assert operation["compaction_version"] == COMPACTION_VERSION
        assert task == json.loads(bodies[1]["messages"][1]["content"])["current_task"]
        assert operation["turn_id"] == task["turn_id"] == report["turns"][-1]["turn_id"]
        assert report["turns"][-1]["tools"][0]["output"] == task["messages"][-1]["content"]
        assert operation["status"] == ("failed" if failure else "succeeded")
        assert (summary is None) == failure
        if summary:
            assert task["turn_id"] not in summary.covered_turn_ids
        store.close()
        conversation.resume(session_id)
        assert store.load_summary(session_id) == summary
        assert len(store.load_turns(session_id)) == (6 if failure else 7)


def test_task_material_is_frozen_budgeted_and_manual_compaction_does_not_invent_a_question(
    tmp_path,
):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=answer("保留原事实"))

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        sid = store.create_session("test", "constraint")
        session = store.select_session(sid)
        for index in range(5):
            store.append_turn(sid, str(index), "facts")
        service = CompactionService(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            ContextConfig(max_input_tokens=3000, safety_margin_tokens=0),
            lambda _: None,
        )
        turn = [{"role": "user", "content": "why?"}]
        plan = service.plan(session, current_turn=turn, turn_id="test-turn")
        turn[0]["content"] = "changed"
        assert plan.current_task["messages"][0]["content"] == "why?"
        material = json.loads(plan.messages[1]["content"])
        assert material["current_task"] == plan.current_task
        assert [p["message_count"] for p in material["source_positions"]] == [2, 2, 2]
        with pytest.raises(BudgetError):
            service.plan(
                session,
                current_turn=[{"role": "user", "content": "超" * 4000}],
                turn_id="test-turn",
            )
        assert bodies == [] and store.load_summary(sid) is None
        summary = service.compact(session, "test")
        material = json.loads(bodies[0]["messages"][1]["content"])
        assert material["current_task"] is None
        assert summary.covered_turn_ids == tuple(t.turn_id for t in store.load_turns(sid)[:3])


@pytest.mark.parametrize("legacy", [False, True])
def test_task_compaction_records_version_and_replays_without_network(tmp_path, monkeypatch, legacy):
    config = replace(
        config_at(tmp_path),
        context=ContextConfig(
            max_input_tokens=20000,
            safety_margin_tokens=0,
            compression_trigger_ratio=0.35,
            compression_target_ratio=0.2,
        ),
    )
    if legacy:
        # 在捕获起点前选择旧行为；缺少版本的旧录制不应产生新摘要请求。
        from adam.recording.runtime import CaptureRuntime

        bind = CaptureRuntime.bind

        def old_bind(self, conversation):
            conversation.compaction_version = None
            bind(self, conversation)

        monkeypatch.setattr(CaptureRuntime, "bind", old_bind)

    def respond(body, number):
        if number == 1:
            material = json.loads(body["messages"][1]["content"])
            assert ("current_task" in material) != legacy
            if not legacy:
                assert material["current_task"]["messages"] == [{"role": "user", "content": "task"}]
            value = answer("summary")
        else:
            value = answer("done")
        return httpx.Response(200, json=value)

    history = [(f"old {index}", "中" * 1000) for index in range(8)]
    with captured_app(tmp_path, respond, config=config, history=history) as (
        app,
        recorder,
        store,
        source,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "done"
        summary = store.load_summary(app.session.session_id)
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "replay")
    assert result["status"] == "matched", result
    with SessionStore(tmp_path / "replay") as replay:
        assert replay.load_summary(replay.list_sessions()[0].session_id) == summary


@pytest.mark.parametrize("fault", ["question", "owner", "tool", "version"])
def test_forged_current_task_cannot_enter_summary_log(tmp_path, fault):
    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("test", "constraint")
        store.select_session(sid)
        store.append_turn(sid, "old", "facts")
        source = store.load_turns(sid)[0].turn_id
        tid = store.start_turn(sid, "actual question")
        payload = {
            "operation_id": "compact",
            "turn_id": tid,
            "model": "test",
            "source_turn_ids": [source],
            "previous_summary_id": None,
            "max_output_tokens": 100,
            "estimated_input_tokens": 200,
            "compaction_version": COMPACTION_VERSION,
            "current_task": {
                "turn_id": tid,
                "messages": [{"role": "user", "content": "actual question"}],
            },
        }
        if fault == "question":
            payload["current_task"]["messages"][0]["content"] = "invented"
        elif fault == "owner":
            payload["current_task"]["turn_id"] = "other"
        elif fault == "tool":
            payload["current_task"]["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_response([call("fake", "read_file", path="fake")])[
                            "choices"
                        ][0]["message"]["tool_calls"],
                    },
                    {"role": "tool", "tool_call_id": "fake", "content": "invented output"},
                ]
            )
        else:
            payload["compaction_version"] = "future"
        before = next(store.root.rglob("*.jsonl")).read_bytes()
        with pytest.raises(ValueError):
            store.append_compaction_event(sid, "compaction_started", payload)
        assert next(store.root.rglob("*.jsonl")).read_bytes() == before
        assert store.load_summary(sid) is None

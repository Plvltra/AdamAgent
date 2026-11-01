"""C00.4：真实本地聊天/摘要/录制只替换远端 HTTP，不验收模型视觉语义。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config.consumption import ConsumptionConfig
from adam.config.settings import ContextConfig, ReflectionConfig
from adam.contracts.consumption import ConsumptionPlan, Quantities, RateCard
from adam.contracts.errors import (
    AgentError,
    BudgetError,
    FailureCode,
    LLMError,
    StorageError,
    TurnSaveError,
)
from adam.recording.journal import read_recording
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_multimodal_input import multimodal_app, wire_bytes
from tests.features.test_reflection_request import SUGGESTION
from tests.features.test_subagents import application as delegated_app
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import forbid_external_actions


def metered(root, **kwargs):
    return replace(config_at(root), consumption=ConsumptionConfig(enabled=True, **kwargs))


def payloads(store, sid, kind):
    return [event["payload"] for event in store.load_events(sid) if event["type"] == kind]


def test_each_physical_request_counts_actual_repeated_image_and_frozen_price(tmp_path):
    (tmp_path / "note").write_text("source", encoding="utf-8")
    price = RateCard(
        "USD",
        (
            ("input_tokens", "0.001"),
            ("output_tokens", "0.002"),
            ("images", "0.1"),
            ("calls", "0.01"),
        ),
    )
    config = metered(tmp_path, max_images=2, max_calls=2, prices=(("test", price),))

    def respond(body, number):
        assert len(wire_bytes(body)) == 1
        return (
            tool_response([call("read", "read_file", path="note")])
            if number == 1
            else answer("done")
        )

    with multimodal_app(tmp_path, respond, config=config) as (app, store, ref, _, bodies, *_):
        assert app.send("task", media=(ref,)) == "done"
        usage = store.consumption_usage(ref.source.session_id)
        assert usage.known == Quantities(20, 4, 2, 0, 2) and usage.unknown_units == ()
        starts = payloads(store, ref.source.session_id, "request_started")
        for event in starts:
            plan = ConsumptionPlan.from_record(event["consumption"])
            assert plan.reservation.images == 1 and plan.price == price
        charges = payloads(store, ref.source.session_id, "request_usage")
        assert [item["consumption"]["charge"]["cost"] for item in charges] == ["0.124"] * 2
        assert store.consumption_report(ref.source.session_id)["costs"] == {
            "USD": {"known_cost": "0.248", "unknown": False}
        }
        with pytest.raises(BudgetError):
            app.send("again", media=(ref,))
        assert (
            len(bodies) == 2 and len(payloads(store, ref.source.session_id, "request_started")) == 2
        )


@pytest.mark.parametrize("media", [False, True])
def test_zero_caps_stop_before_http_and_release_token_and_unit_reservations(tmp_path, media):
    config = metered(tmp_path, max_calls=0, max_images=0)
    with multimodal_app(tmp_path, lambda *_: pytest.fail("must not send"), config=config) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        with pytest.raises(BudgetError):
            app.send("task", media=(ref,) if media else ())
        assert bodies == [] and not payloads(store, ref.source.session_id, "request_started")


def test_image_exhaustion_does_not_block_a_text_only_request_without_old_image_history(tmp_path):
    # 原图只保存为证据而未进入模型历史；零图片上限不能变成禁用全部模型请求。
    with multimodal_app(tmp_path, lambda *_: answer(), config=metered(tmp_path, max_images=0)) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        assert app.send("task") == "done"
        assert not wire_bytes(bodies[0])
        assert store.consumption_usage(ref.source.session_id).known == Quantities(10, 2, 0, 0, 1)


@pytest.mark.parametrize("boundary", ["unknown", "cancel", "not-sent", "usage-missing"])
def test_failure_counts_attempt_and_preserves_unknown_instead_of_retrying_image(tmp_path, boundary):
    def respond(*_):
        if boundary == "cancel":
            raise KeyboardInterrupt()
        if boundary == "not-sent":
            raise httpx.ConnectError("injected connection establishment boundary")
        if boundary == "unknown":
            raise httpx.ReadError("injected after-send response boundary")
        value = answer()
        value.pop("usage")
        return value

    with multimodal_app(tmp_path, respond, config=metered(tmp_path, max_calls=1)) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        with pytest.raises((LLMError, BudgetError, KeyboardInterrupt)):
            app.send("task", media=(ref,))
        usage = store.consumption_usage(ref.source.session_id)
        assert len(bodies) == 1 and usage.known.calls == 1 and usage.known.audio_ms == 0
        if boundary == "not-sent":
            assert usage.known == Quantities(0, 0, 0, 0, 1) and not usage.unknown_units
        else:
            assert set(usage.unknown_units) == (
                {"input_tokens", "output_tokens"}
                if boundary == "usage-missing"
                else {"input_tokens", "output_tokens", "images"}
            )
            assert usage.known.images == (1 if boundary == "usage-missing" else 0)
        with pytest.raises(BudgetError):
            app.send("must not resend", media=(ref,))
        assert (
            len(bodies) == 1
            and not store.load_turns(ref.source.session_id)[-1].messages[-1]["content"] == "done"
        )


@pytest.mark.parametrize("boundary", ["settlement", "final"])
def test_save_failure_keeps_known_or_pending_units_without_double_settlement(
    tmp_path, monkeypatch, boundary
):
    with multimodal_app(tmp_path, lambda *_: answer("partial"), config=metered(tmp_path)) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):

        def fail(*args, **kwargs):
            raise StorageError("injected original storage write boundary")

        monkeypatch.setattr(
            store, "finish_request" if boundary == "settlement" else "append_messages", fail
        )
        with pytest.raises(TurnSaveError) as error:
            app.send("task", media=(ref,))
        assert error.value.partial_reply == "partial" and len(bodies) == 1
        usage = store.consumption_usage(ref.source.session_id)
        if boundary == "settlement":
            assert set(usage.unknown_units) == {
                "input_tokens",
                "output_tokens",
                "images",
                "audio_ms",
                "calls",
            }
        else:
            assert usage.known == Quantities(10, 2, 1, 0, 1) and not usage.unknown_units
        assert store.consumption_usage(ref.source.session_id) == usage


def test_manual_summary_uses_same_call_cap_and_does_not_create_chat_input(tmp_path):
    config = metered(tmp_path, max_calls=1)
    config = replace(config, context=ContextConfig(auto_compact=False, retain_recent_turns=0))
    with multimodal_app(tmp_path, lambda *_: answer("summary"), config=config) as (
        app,
        store,
        ref,
        _,
        bodies,
        *_,
    ):
        before = len(payloads(store, ref.source.session_id, "turn_started"))
        assert app.compact().text == "summary"
        start = payloads(store, ref.source.session_id, "compaction_started")[-1]
        assert start["consumption"]["purpose"] == "summary"
        assert start["consumption"]["reservation"]["images"] == len(wire_bytes(bodies[0])) == 0
        assert store.consumption_usage(ref.source.session_id).known == Quantities(10, 2, 0, 0, 1)
        with pytest.raises(BudgetError):
            app.send("task")
        assert len(bodies) == 1
        assert len(payloads(store, ref.source.session_id, "turn_started")) == before + 1


def test_metered_media_recording_replays_real_flow_without_original_or_network(
    tmp_path, monkeypatch
):
    config = metered(tmp_path, max_images=1, max_calls=1)
    with multimodal_app(tmp_path, lambda *_: answer("frozen"), config=config, capture=True) as (
        app,
        store,
        ref,
        _,
        _,
        recorder,
        path,
    ):
        assert recorder.run(lambda: app.send("task", media=(ref,))) == "frozen"
        original_usage = store.consumption_usage(ref.source.session_id)
    recording = read_recording(path)
    assert recording.complete
    assert any(
        event["type"] == "observation"
        and event["data"]["channel"] == "accounting"
        and "units" in (event["data"]["value"].get("value") or {})
        for event in recording.events
    )
    source = (tmp_path / "sessions").resolve()
    hidden = (tmp_path / "source-unavailable").resolve()
    assert source.is_relative_to(tmp_path.resolve()) and hidden.is_relative_to(tmp_path.resolve())
    source.rename(hidden)
    forbid_external_actions(monkeypatch)
    report = replay_turn(path, tmp_path / "offline")
    assert report["status"] == "matched", report
    with SessionStore(tmp_path / "offline") as restored:
        restored.select_session(ref.source.session_id)
        assert restored.consumption_usage(ref.source.session_id) == original_usage


@pytest.mark.parametrize("unknown_child", [False, True])
def test_actual_child_and_parent_share_calls_and_restart_restores_without_double_charge(
    tmp_path, unknown_child
):
    from adam.application.subagents.account import restore_account

    children, parents = [], []
    config = metered(tmp_path, max_calls=4)

    def child(request):
        children.append(json.loads(request.content))
        if unknown_child:
            raise httpx.ReadError("injected child response after-send boundary")
        return httpx.Response(200, json=answer("child complete"))

    def parent(request):
        body = json.loads(request.content)
        parents.append(body)
        if len(parents) == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="text task")])
        elif len(parents) == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[], timeout_seconds=10)])
        else:
            assert outputs(body)
            result = answer("parent complete")
        return httpx.Response(200, json=result)

    with delegated_app(tmp_path, parent, child, config=config) as (app, store):
        if unknown_child:
            with pytest.raises(BudgetError) as error:
                app.send("task")
            assert error.value.code == FailureCode.USAGE_UNKNOWN
        else:
            assert app.send("task") == "parent complete"
        sid = app.session.session_id
        with pytest.raises(BudgetError):
            app.send("no more calls")
        assert len(children) == 1
        own = store.consumption_usage(sid)
    with SessionStore(config.sessions_dir) as store:
        store.select_session(sid)
        account = restore_account(store, sid, config.agent.max_session_tokens, config.consumption)
        first = account.budget.units.usage()
        account.usage()
        account.usage()
        assert account.budget.units.usage() == first
        assert first.known.calls == len(children) + len(parents)
        if unknown_child:
            assert set(first.unknown_units) == {"input_tokens", "output_tokens"}
        else:
            assert first.known.calls == 4 and not first.unknown_units
        with pytest.raises(BudgetError), account.request(10, units=Quantities(calls=1)):
            pass
        assert store.consumption_usage(sid) == own


@pytest.mark.parametrize("invalid_protocol", [False, True])
def test_real_reflection_request_shares_media_call_limits_and_preserves_degradation(
    tmp_path, invalid_protocol
):
    config = replace(
        metered(tmp_path, max_calls=3, max_images=3),
        reflection=ReflectionConfig(max_input_tokens=20000),
    )

    def respond(body, number):
        assert len(wire_bytes(body)) == 1
        if number == 1:
            return tool_response([call("missing", "read_file", path="missing")])
        if number == 2:
            assert "tools" not in body
            value = answer(json.dumps(SUGGESTION))
            if invalid_protocol:
                value["choices"] = []
            return value
        return answer("answer after reflection")

    with multimodal_app(tmp_path, respond, config=config) as (app, store, ref, _, bodies, *_):
        assert app.send("task", media=(ref,)) == "answer after reflection"
        assert len(bodies) == 3
        usage = store.consumption_usage(ref.source.session_id)
        assert usage.known == Quantities(30, 6, 3, 0, 3) and not usage.unknown_units
        request = payloads(store, ref.source.session_id, "request_started")[1]
        assert request["context"]["strategy"] == "reflection_v1"
        assert store.load_reflections(ref.source.session_id)[0]["status"] == (
            "invalid" if invalid_protocol else "completed"
        )


def test_legacy_unmetered_media_does_not_become_known_zero_on_enable(tmp_path):
    with multimodal_app(tmp_path, lambda *_: answer()) as (app, store, ref, _, _, *_):
        app.send("old", media=(ref,))
        sid = ref.source.session_id
    from adam.application.subagents.account import restore_account

    with SessionStore(tmp_path / "sessions") as store:
        store.select_session(sid)
        account = restore_account(store, sid, 100000, ConsumptionConfig(enabled=True))
        assert account.budget.units.usage().known.calls == 1
        with pytest.raises(BudgetError) as error:
            account.check()
        assert error.value.code == FailureCode.USAGE_UNKNOWN
        assert set(account.budget.units.usage().unknown_units) == {"images", "audio_ms"}


def test_late_local_image_validation_is_zero_submission_but_one_adapter_attempt(
    tmp_path, monkeypatch
):
    from tests.features.test_media_evidence import media_path

    with multimodal_app(
        tmp_path,
        lambda *_: pytest.fail("late invalid input must not send"),
        config=metered(tmp_path),
    ) as (app, store, ref, media, bodies, *_):
        start = store.start_request

        def corrupt(*args, **kwargs):
            result = start(*args, **kwargs)
            media_path(media, ref).write_bytes(b"injected corrupt bytes after preflight")
            return result

        monkeypatch.setattr(store, "start_request", corrupt)
        with pytest.raises(AgentError) as error:
            app.send("task", media=(ref,))
        assert error.value.code == FailureCode.MODEL_INPUT_INVALID and not bodies
        usage = store.consumption_usage(ref.source.session_id)
        assert usage.known == Quantities(0, 0, 0, 0, 1) and not usage.unknown_units


def test_request_uses_price_frozen_before_http_even_if_host_replaces_runtime_config(tmp_path):
    old = RateCard(
        "USD", (("calls", "0.1"), ("input_tokens", "0"), ("output_tokens", "0"), ("images", "0.2"))
    )
    new = RateCard("CNY", (("calls", "999"),))
    config = metered(tmp_path, prices=(("test", old),))

    def respond(*_):
        # 故障注入点是调用前已保存计划之后的宿主配置变化，不是供应商自报价格。
        object.__setattr__(config.consumption, "prices", (("test", new),))
        return answer()

    with multimodal_app(tmp_path, respond, config=config) as (app, store, ref, _, _, *_):
        assert app.send("task", media=(ref,)) == "done"
        usage = payloads(store, ref.source.session_id, "request_usage")[-1]["consumption"]
        assert usage["charge"]["currency"] == "USD" and usage["charge"]["cost"] == "0.3"


@pytest.mark.parametrize("change", ["parent", "reserved", "caps"])
def test_replay_does_not_accept_forged_units_or_recorded_success_over_current_cap(
    tmp_path, monkeypatch, change
):
    from adam.recording.journal import parse_recording
    from adam.recording.replay import ReplayTape
    from adam.recording.replay_store import ReplayStore
    from adam.recording.runtime import build_replay

    with multimodal_app(
        tmp_path, lambda *_: answer("frozen"), config=metered(tmp_path), capture=True
    ) as (app, _, ref, _, _, recorder, path):
        recorder.run(lambda: app.send("task", media=(ref,)))
    recording = read_recording(path)
    # 修改冻结的决策输入，不篡改会话事件或声称服务端响应；应在重放模型前拒绝。
    for event in recording.events:
        if event["type"] != "observation":
            continue
        data = event["data"]
        if (
            change == "caps"
            and data["channel"] == "context"
            and data["value"]["operation"] == "runtime"
        ):
            data["value"]["value"]["consumption"]["max_calls"] = 0
            break
        if data["channel"] == "accounting" and data["value"]["operation"] == (
            "check" if change == "parent" else "reserve"
        ):
            state = data["value"]["value"]["units"]
            if change == "parent":
                state["members"][0]["known"]["images"] += 1
            else:
                state["reserved"]["calls"] = None
            break
    # 原旁路副本已由 read_recording 验证；仅重新解析修改后的 JSON 结构，不读取外部媒体。
    parsed = parse_recording("".join(json.dumps(e) + "\n" for e in recording.events).encode())
    parsed = replace(parsed, media_verified=True)
    forbid_external_actions(monkeypatch)
    tape = ReplayTape(parsed)
    with ReplayStore(tmp_path / "forged-offline", tape) as store:
        conversation, selection = build_replay(tape, store)
        with pytest.raises(AgentError):
            conversation.send("task", selected_skill=selection, media=(ref,))
        assert not payloads(store, ref.source.session_id, "request_started")


def test_last_call_tool_response_stops_before_tool_side_effect_or_second_request(tmp_path):
    (tmp_path / "note").write_text("unchanged", encoding="utf-8")
    with multimodal_app(
        tmp_path,
        lambda *_: tool_response([call("read", "read_file", path="note")]),
        config=metered(tmp_path, max_calls=1),
    ) as (app, store, ref, _, bodies, *_):
        with pytest.raises(BudgetError):
            app.send("task")
        assert len(bodies) == 1
        assert not payloads(store, ref.source.session_id, "tool_started")
        assert store.consumption_usage(ref.source.session_id).known.calls == 1
        assert (tmp_path / "note").read_text(encoding="utf-8") == "unchanged"


def test_replay_uses_settlement_time_other_member_unknown_not_start_snapshot(tmp_path, monkeypatch):
    from adam.contracts.consumption import ConsumptionSnapshot
    from adam.recording.runtime import CaptureRuntime
    from tests.features.test_turn_replay import captured_app

    budgets, initialized = [], set()
    bind_runtime = CaptureRuntime.bind

    def bind(runtime, conversation):
        factory = conversation.accounting

        def account(sid):
            selected = factory(sid)
            if id(selected.budget) not in initialized:
                initialized.add(id(selected.budget))
                selected.budget.update(
                    "other-member", 0, False, units=ConsumptionSnapshot(Quantities())
                )
            budgets.append(selected.budget)
            return selected

        conversation.accounting = account
        conversation.requests.accounting = account
        bind_runtime(runtime, conversation)

    monkeypatch.setattr(CaptureRuntime, "bind", bind)

    def respond(*_):
        # 在原模型 IO 返回前注入另一个成员刚发布的未知图片消费；不篡改主日志。
        budgets[-1].update(
            "other-member", 0, False, units=ConsumptionSnapshot(Quantities(calls=1), ("images",))
        )
        return httpx.Response(200, json=answer("not adopted"))

    with captured_app(tmp_path, respond, config=metered(tmp_path)) as (
        app,
        recorder,
        store,
        path,
        _,
    ):
        with pytest.raises(BudgetError) as error:
            recorder.run(lambda: app.send("task"))
        assert error.value.code == FailureCode.USAGE_UNKNOWN
        sid = app.session.session_id
        assert store.consumption_usage(sid).known.calls == 1
        assert payloads(store, sid, "request_finished")[-1]["status"] == "succeeded"
        assert payloads(store, sid, "turn_finished")[-1]["reason"] == "usage_unknown"
    recording = read_recording(path)
    assert any(
        event["type"] == "observation"
        and event["data"]["channel"] == "accounting"
        and event["data"]["value"]["operation"] == "settled"
        for event in recording.events
    )
    forbid_external_actions(monkeypatch)
    report = replay_turn(path, tmp_path / "settled-offline")
    assert report["status"] == "matched", report
    assert report["error"]["code"] == "usage_unknown"

"""C00.4 存储边界：真实 JSONL/重启/诊断与故障回滚；应用自动计量接入另验收。"""

from dataclasses import replace

import pytest

from adam.contracts.consumption import ConsumptionPlan, Quantities, RateCard
from adam.contracts.errors import AgentError, RequestInterrupted, StorageError
from adam.session.diagnostics import read_report
from adam.session.outcome import ExecutionOutcome
from adam.session.storage import SessionStore


def plan(currency="USD"):
    return ConsumptionPlan(
        Quantities(100, 50, 2, 1500, 1),
        price=RateCard(
            currency,
            (
                ("input_tokens", "0.001"),
                ("output_tokens", "0.002"),
                ("images", "0.1"),
                ("audio_ms", "0.00001"),
                ("calls", "0.01"),
            ),
        ),
    )


def start(store, sid, selected=None):
    turn = store.start_turn(sid, "question")
    selected = selected or plan()
    request = store.start_request(sid, turn, "host-selected", consumption=selected)
    return turn, request, selected


def test_units_and_configured_estimate_survive_restart_and_read_only_diagnostics(tmp_path):
    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("units", "constraint")
        turn, request, selected = start(store, sid)
        actual = Quantities(3, 2, 2, 1234, 1)
        store.finish_request(sid, turn, request, (3, 2), consumption=selected.settlement(actual))
        store.append_messages(
            sid,
            turn,
            [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}],
        )
        assert store.usage(sid) == (5, False)
        snapshot = store.consumption_usage(sid)
        assert snapshot.known == actual and snapshot.unknown_units == ()
        path = next(store.root.glob("*/*/*/*.jsonl"))
        before = path.read_bytes()
        report = read_report(path)
        assert report["consumption"] == store.consumption_report(sid)
        assert report["consumption"]["costs"] == {
            "USD": {"known_cost": "0.22934", "unknown": False}
        }
        assert report["turns"][0]["requests"][0]["consumption_usage"] == selected.settlement(actual)
        assert path.read_bytes() == before
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.consumption_usage(sid) == snapshot
        assert path.read_bytes() == before


def test_unpriced_and_different_currencies_remain_separate(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("prices", "system")
        for selected in (plan("USD"), plan("CNY"), replace(plan(), price=None)):
            turn, request, _ = start(store, sid, selected)
            amounts = Quantities(3, 2, 1, 1000, 1)
            store.finish_request(
                sid, turn, request, (3, 2), consumption=selected.settlement(amounts)
            )
            store.finish_turn(sid, turn, ExecutionOutcome.failure(AgentError("after response")))
        report = store.consumption_report(sid)
        assert report["known"] == Quantities(9, 6, 3, 3000, 3).as_record()
        assert report["unpriced_requests"] == 1
        assert report["costs"] == {
            "USD": {"known_cost": "0.127", "unknown": False},
            "CNY": {"known_cost": "0.127", "unknown": False},
        }


@pytest.mark.parametrize("boundary", ["duplicate", "cost", "token", "missing", "extra"])
def test_invalid_or_duplicate_settlement_never_changes_persisted_totals(tmp_path, boundary):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("atomic", "system")
        turn, request, selected = start(store, sid)
        amounts = Quantities(3, 2, 1, 0, 1)
        settled = selected.settlement(amounts)
        if boundary == "duplicate":
            store.finish_request(sid, turn, request, (3, 2), consumption=settled)
        elif boundary == "cost":
            settled["charge"]["cost"] = "0"
        elif boundary == "token":
            settled["amounts"]["input_tokens"] = 4
        elif boundary == "missing":
            settled = None
        else:
            settled["amounts"]["seconds"] = 0
        before, report = store.load_events(sid), store.consumption_report(sid)
        with pytest.raises(ValueError):
            store.finish_request(sid, turn, request, (3, 2), consumption=settled)
        assert store.load_events(sid) == before and store.consumption_report(sid) == report


def test_old_token_logs_do_not_invent_zero_media_units_or_prices(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("legacy", "system")
        turn = store.start_turn(sid, "legacy")
        request = store.start_request(sid, turn, "text")
        store.finish_request(sid, turn, request, (3, 2))
        report = store.consumption_report(sid)
        assert report["known"] == Quantities(3, 2, 0, 0, 1).as_record()
        assert report["unknown_units"] == ["images", "audio_ms"]
        assert report["costs"] == {} and report["unpriced_requests"] == 1
        with pytest.raises(ValueError):
            second = store.start_request(sid, turn, "text")
            store.finish_request(
                sid, turn, second, (3, 2), consumption=plan().settlement(Quantities(3, 2, 0, 0, 1))
            )


def test_cancel_keeps_known_calls_but_does_not_guess_tokens_or_audio(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("cancel", "system")
        turn, request, selected = start(store, sid)
        unknown = Quantities(None, None, None, None, 1)
        outcome = ExecutionOutcome.failure(RequestInterrupted())
        store.finish_request(
            sid, turn, request, None, consumption=selected.settlement(unknown), outcome=outcome
        )
        store.finish_turn(sid, turn, outcome)
        snapshot = store.consumption_usage(sid)
        assert snapshot.known.calls == 1 and snapshot.unknown_units == (
            "input_tokens",
            "output_tokens",
            "images",
            "audio_ms",
        )
        assert store.consumption_report(sid)["costs"]["USD"] == {
            "known_cost": "0.01",
            "unknown": True,
        }
        assert store.load_messages(sid) == []


def test_restart_of_unsettled_request_preserves_unknown_units_and_does_not_resend(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("restart", "system")
        start(store, sid)
    with SessionStore(tmp_path) as reopened:
        reopened.select_session(sid)
        report = reopened.consumption_report(sid)
        assert report["known"] == Quantities().as_record()
        assert report["unknown_units"] == list(Quantities().as_record())
        assert (
            len(
                [event for event in reopened.load_events(sid) if event["type"] == "request_started"]
            )
            == 1
        )
        assert (
            read_report(next(tmp_path.glob("*/*/*/*.jsonl")))["turns"][0]["status"] == "interrupted"
        )


def test_settlement_write_failure_rolls_back_units_and_costs_together(tmp_path, monkeypatch):
    """在真实请求结算 fsync 边界失败，不能仅保留单位/费用中的一部分。"""
    with SessionStore(tmp_path) as store:
        sid = store.create_session("disk", "system")
        turn, request, selected = start(store, sid)
        before = store.load_events(sid)

        def fail(_):
            raise OSError("injected fsync boundary")

        with monkeypatch.context() as patch:
            patch.setattr("adam.session.storage.os.fsync", fail)
            with pytest.raises(StorageError):
                store.finish_request(
                    sid,
                    turn,
                    request,
                    (3, 2),
                    consumption=selected.settlement(Quantities(3, 2, 1, 0, 1)),
                )
        assert store.load_events(sid) == before
        assert store.consumption_usage(sid).known == Quantities()


def test_manual_summary_uses_same_unit_projection_without_fake_user_request(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("summary", "system")
        store.append_turn(sid, "old", "answer")
        selected = replace(plan(), purpose="summary")
        source = store.load_turns(sid)[0].turn_id
        store.append_compaction_event(
            sid,
            "compaction_started",
            {
                "operation_id": "summary-1",
                "turn_id": None,
                "model": "summary-model",
                "source_turn_ids": [source],
                "previous_summary_id": None,
                "estimated_input_tokens": 100,
                "max_output_tokens": 50,
                "consumption": selected.as_record(),
            },
        )
        store.append_compaction_event(
            sid,
            "compaction_usage",
            {
                "operation_id": "summary-1",
                "input_tokens": 3,
                "output_tokens": 2,
                "consumption": selected.settlement(Quantities(3, 2, 0, 0, 1)),
                **ExecutionOutcome.success(10).payload(),
            },
        )
        store.append_compaction_event(
            sid,
            "compaction_completed",
            {
                "operation_id": "summary-1",
                "text": "compressed",
                **ExecutionOutcome.success(11).payload(),
            },
        )
        assert store.usage(sid) == (5, False)
        assert store.consumption_usage(sid).known == Quantities(3, 2, 0, 0, 1)
        assert len(store.load_turns(sid)) == 1 and store.load_summary(sid).text == "compressed"


def test_legacy_large_known_token_count_keeps_original_log_contract(tmp_path):
    with SessionStore(tmp_path) as store:
        sid = store.create_session("legacy-large", "system")
        turn = store.start_turn(sid, "legacy")
        request = store.start_request(sid, turn, "text")
        store.finish_request(sid, turn, request, (10**19, 2))
        assert store.usage(sid) == (10**19 + 2, False)
        assert store.consumption_usage(sid).known.input_tokens == 10**19

"""E5.1：审查来源、独立读取依据及重启状态，用真实临时 JSONL 验证。"""

import copy

import pytest

from adam.contracts.review import validate_result
from adam.session.storage import SessionStore
from tests.features.test_task_plan_storage import begin_tool, finish_tool


def requested(store):
    sid = store.create_session("review", "")
    store.select_session(sid)
    tid = store.start_turn(sid, "review code")
    rid = store.start_request(sid, tid, "test")
    store.finish_request(sid, tid, rid, (1, 1))
    begin_tool(store, sid, tid, rid, "ask", "request_review")
    request = {
        "review_id": "review1",
        "turn_id": tid,
        "request_id": rid,
        "tool_call_id": "ask",
        "agent_id": "11223344",
        "run_id": "run1",
        "round": 1,
        "max_rounds": 2,
        "goal": "addition",
        "criteria": "2+3=5",
        "targets": [{"path": "calc.py", "version": "a" * 64}],
    }
    store.append_review(sid, "review_requested", request)
    finish_tool(store, sid, tid, rid, "ask")
    return sid, request


def result():
    return {
        "verdict": "findings",
        "summary": "加法用了减法",
        "evidence": ["read"],
        "findings": [
            {
                "path": "calc.py",
                "line": 2,
                "severity": "high",
                "message": "应按需求执行加法",
                "evidence_id": "read",
            }
        ],
    }


def reads():
    return [
        {
            "tool_call_id": "read",
            "path": "calc.py",
            "version": "a" * 64,
            "lines": [1, 2],
            "complete": True,
        }
    ]


def test_pending_review_recovers_as_interrupted_without_replay(tmp_path):
    with SessionStore(tmp_path) as store:
        sid, request = requested(store)
    with SessionStore(tmp_path) as store:
        store.select_session(sid)
        reviews = store.load_reviews(sid)
        assert reviews[0]["status"] == "interrupted"
        assert store.load_messages(sid) == []
        assert len([e for e in store.load_events(sid) if e["type"] == "request_started"]) == 1


def test_complete_review_preserves_model_verdict_and_actual_read_receipts(tmp_path):
    with SessionStore(tmp_path) as store:
        sid, req = requested(store)
        begin_tool(store, sid, req["turn_id"], req["request_id"], "collect", "collect_review")
        record = {k: req[k] for k in ("review_id", "turn_id", "request_id")}
        record.update(
            tool_call_id="collect",
            status="completed",
            reason="reviewed",
            child_turn_id="childturn",
            reads=reads(),
            result=result(),
        )
        store.append_review(sid, "review_finished", record)
        assert store.load_reviews(sid)[0]["result"] == result()
        assert store.load_messages(sid) == []


@pytest.mark.parametrize("change", ["fake", "line", "version", "unread", "contradiction"])
def test_review_rejects_unread_or_foreign_evidence(change):
    value, receipts = result(), reads()
    if change == "fake":
        value["evidence"] = ["invented"]
    elif change == "line":
        value["findings"][0]["line"] = 99
    elif change == "version":
        receipts[0]["version"] = "b" * 64
    elif change == "unread":
        receipts.clear()
    else:
        value["verdict"] = "no_findings"
    with pytest.raises(ValueError):
        validate_result(value, receipts, [{"path": "calc.py", "version": "a" * 64}])


def test_partial_read_cannot_claim_no_findings():
    receipts = reads()
    receipts[0]["complete"] = False
    report = {"verdict": "no_findings", "summary": "ok", "evidence": ["read"], "findings": []}
    with pytest.raises(ValueError):
        validate_result(report, receipts, [{"path": "calc.py", "version": "a" * 64}])


def test_parallel_or_duplicate_review_cannot_be_appended(tmp_path):
    with SessionStore(tmp_path) as store:
        sid, request = requested(store)
        begin_tool(store, sid, request["turn_id"], request["request_id"], "again", "request_review")
        second = copy.deepcopy(request)
        second.update(review_id="review2", tool_call_id="again", round=2)
        with pytest.raises(ValueError):
            store.append_review(sid, "review_requested", second)
        assert len(store.load_reviews(sid)) == 1

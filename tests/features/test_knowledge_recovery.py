"""B12.3：真实竞争与强杀恢复，状态决定、模型请求和冻结来源分别核对。"""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adam.documents.knowledge_access import KnowledgeAccess
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.models import DocumentError
from adam.documents.snapshots import load_snapshot
from tests.features.support.knowledge_worker import lifecycle_at
from tests.features.test_knowledge_candidates import proposal_fixture
from tests.features.test_knowledge_publication import Reviewer


@pytest.fixture
def knowledge(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    lifecycle = KnowledgeLifecycle(KnowledgeProposals(tmp_path / "knowledge", index))
    candidate = lifecycle.proposals.create(proposal)
    return lifecycle, candidate["candidate_id"]


def start(root, identity, action, *, name="worker", phase=""):
    job = {
        "root": str(root),
        "candidate_id": identity,
        "action": action,
        "phase": phase,
        "marker": str(root / (name + ".ready")),
        "release": str(root / (name + ".go")),
        "shown": str(root / (name + ".shown")),
    }
    path = root / (name + ".job")
    path.write_text(json.dumps(job), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.knowledge_worker", str(path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return process


def wait_ready(root, process, name="worker"):
    deadline = time.monotonic() + 15
    while not (root / (name + ".ready")).exists():
        if process.poll() is not None:
            output, error = process.communicate()
            pytest.fail(f"worker exited before barrier: {output} {error}")
        if time.monotonic() > deadline:
            pytest.fail("worker did not reach requested boundary")
        time.sleep(0.01)


def finish(process):
    output, error = process.communicate(timeout=15)
    assert process.returncode == 0, error
    return json.loads(output)


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


def run(root, identity, action, name="immediate"):
    process = start(root, identity, action, name=name)
    try:
        return finish(process)
    finally:
        stop(process)


def calls(root):
    path = root / "http-calls.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.parametrize("change", ["publish", "reject", "source"])
def test_independent_confirmation_cannot_overwrite_a_concurrent_decision_or_source(
    tmp_path, knowledge, change
):
    lifecycle, identity = knowledge
    lifecycle.review(identity, Reviewer())
    frozen = (lifecycle.proposals.directory / (identity + ".json")).read_bytes()
    process = start(tmp_path, identity, "publish", phase="confirm")
    try:
        wait_ready(tmp_path, process)
        shown = json.loads((tmp_path / "worker.shown").read_text(encoding="utf-8"))
        assert shown["state"]["status"] == "reviewed"
        assert shown["candidate"]["proposal"]["counterevidence"]
        if change == "source":
            (tmp_path / "docs/new.md").write_text("新版本改成三次。", encoding="utf-8")
        else:
            assert run(tmp_path, identity, change)["ok"]
        (tmp_path / "worker.go").touch()
        result = finish(process)
        assert not result["ok"]
        state = lifecycle_at(tmp_path).inspect(identity)["state"]
        assert (
            state["status"]
            == {"publish": "published", "reject": "rejected", "source": "reviewed"}[change]
        )
        assert len(lifecycle.history(identity)) == (1 if change == "source" else 2)
        assert (lifecycle.proposals.directory / (identity + ".json")).read_bytes() == frozen
        assert calls(tmp_path) == []
    finally:
        stop(process)


def test_independent_rejection_while_http_is_pending_keeps_cost_but_discards_review(
    tmp_path, knowledge
):
    lifecycle, identity = knowledge
    process = start(tmp_path, identity, "review", phase="transport")
    try:
        wait_ready(tmp_path, process)
        assert run(tmp_path, identity, "reject")["ok"]
        (tmp_path / "worker.go").touch()
        assert not finish(process)["ok"]
        result = run(tmp_path, identity, "inspect")["result"]
        assert result["view"]["state"]["status"] == "rejected"
        assert result["current"]["items"] == []
        assert [entry["status"] for entry in result["history"]] == ["rejected"]
        assert result["requests"][0]["state"] == "succeeded"
        assert result["requests"][0]["usage"] == [10, 5]
        assert len(calls(tmp_path)) == 1
        assert lifecycle.inspect(identity)["state"]["review"] is None
    finally:
        stop(process)


@pytest.mark.parametrize(
    "phase", ["started_after", "transport", "succeeded_after", "reviewed_after"]
)
def test_killed_real_review_process_does_not_resend_or_publish_on_restart(
    tmp_path, knowledge, phase
):
    lifecycle, identity = knowledge
    frozen = (lifecycle.proposals.directory / (identity + ".json")).read_bytes()
    process = start(tmp_path, identity, "review", phase=phase)
    try:
        wait_ready(tmp_path, process)
        stop(process)
        before = {p: p.read_bytes() for p in lifecycle.proposals.root.rglob("*.json")}
        result = run(tmp_path, identity, "inspect")["result"]
        assert result["view"]["state"]["status"] == (
            "reviewed" if phase == "reviewed_after" else "pending"
        )
        assert result["current"]["items"] == []
        record = result["requests"][0]
        known = phase in {"succeeded_after", "reviewed_after"}
        assert record["state"] == ("succeeded" if known else "started")
        assert record["unknown_usage"] is not known
        assert record["usage"] == ([10, 5] if known else None)
        assert len(calls(tmp_path)) == (0 if phase == "started_after" else 1)
        assert {p: p.read_bytes() for p in before} == before
        assert (lifecycle.proposals.directory / (identity + ".json")).read_bytes() == frozen
    finally:
        stop(process)


@pytest.mark.parametrize("action", ["publish", "withdraw", "current"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_killed_state_write_keeps_whole_old_or_new_decision_and_releases_lock(
    tmp_path, knowledge, action, boundary
):
    lifecycle, identity = knowledge
    lifecycle.review(identity, Reviewer())
    if action != "publish":
        lifecycle.publish(identity, lambda view: True)
    if action == "current":
        (tmp_path / "docs/new.md").write_text("新来源变成三次。", encoding="utf-8")
    previous = lifecycle.inspect(identity)["state"]["status"]
    old = lifecycle.path.read_bytes()
    target = {"publish": "published", "withdraw": "withdrawn", "current": "needs_review"}[action]
    process = start(tmp_path, identity, action, phase=target + "_" + boundary)
    try:
        wait_ready(tmp_path, process)
        stop(process)
        # inspect 首先读取已有决定，再由 current 检查来源；两种状态不混为一次写入。
        result = run(tmp_path, identity, "inspect")["result"]
        assert result["view"]["state"]["status"] == (target if boundary == "after" else previous)
        if action != "current":
            assert bool(result["current"]["items"]) == (
                (action == "publish" and boundary == "after")
                or (action == "withdraw" and boundary == "before")
            )
        else:
            assert result["current"]["items"] == []
            assert lifecycle.inspect(identity)["state"]["status"] == "needs_review"
        if boundary == "before" and action != "current":
            assert lifecycle.path.read_bytes() == old
        ledger = load_snapshot(lifecycle.path)
        assert all(event["candidate_id"] == identity for event in ledger["events"])
        assert calls(tmp_path) == []
        assert lifecycle.proposals.read(identity)["status"] == "pending"
    finally:
        stop(process)


def test_conflicting_candidates_require_separate_reviews_and_decisions_after_restart(
    tmp_path, knowledge
):
    lifecycle, first = knowledge
    original = lifecycle.proposals.read(first)
    opposite = copy.deepcopy(original["proposal"])
    opposite.update(
        statement="旧版本仍采用无限重试。",
        applicability="仅适用于旧版本。",
        reasoning="两份规则适用版本不同，不能抹去旧版本的规则。",
        valid_from=None,
        support=original["proposal"]["counterevidence"],
        counterevidence=original["proposal"]["support"],
    )
    second = lifecycle.proposals.create(opposite)["candidate_id"]
    rejected = lifecycle.proposals.create(opposite)["candidate_id"]
    lifecycle.review(first, Reviewer())
    lifecycle.publish(first, lambda view: True)
    lifecycle.reject(rejected, "证据不足，不采用重复候选")
    assert [v["candidate"]["candidate_id"] for v in lifecycle.current()] == [first]
    reopened = lifecycle_at(tmp_path)
    assert reopened.inspect(second)["state"]["status"] == "pending"
    reopened.review(second, Reviewer())
    assert len(reopened.current()) == 1
    reopened.publish(second, lambda view: True)
    facts = KnowledgeAccess(reopened).query()["items"]
    assert {item["candidate_id"] for item in facts} == {first, second}
    assert all(item["counterevidence"] for item in facts)
    reopened.withdraw(first, "新规则需要重新核对")
    assert [v["candidate"]["candidate_id"] for v in reopened.current()] == [second]
    with pytest.raises(DocumentError):
        reopened.publish(rejected, lambda view: pytest.fail("rejected candidate cannot ask again"))
    assert reopened.proposals.read(first) == original
    assert calls(tmp_path) == []

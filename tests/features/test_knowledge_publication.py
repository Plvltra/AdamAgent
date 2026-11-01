"""B12.2.1：真实来源与状态文件，模拟独立审查意见及用户确认边界。"""

import copy
from datetime import UTC, datetime

import pytest
from filelock import FileLock

from adam.contracts.errors import ToolError
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.models import DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from tests.features.test_knowledge_candidates import proposal_fixture


class Reviewer:
    """只替代审查意见来源；候选、证据、状态及文件锁均走真实实现。"""

    identity = "fixture-reviewer-v1"

    def __init__(self, callback=None, verdict="accept"):
        self.callback = callback
        self.verdict = verdict
        self.calls = []

    def review(self, candidate, review_id):
        self.calls.append((copy.deepcopy(candidate), review_id))
        proposal = candidate["proposal"]
        opinion = {
            "verdict": self.verdict,
            "summary": "新规则有明确适用版本，仍由用户核对。",
            "counterevidence_assessment": "旧方案与新版本冲突，不能推定所有版本都改为两次。",
            "evidence_ids": sorted(
                {c["chunk_id"] for c in proposal["support"] + proposal["counterevidence"]}
            ),
        }
        if self.callback:
            self.callback(candidate, opinion)
        return opinion


@pytest.fixture
def publication(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    candidate = store.create(proposal)
    return KnowledgeLifecycle(store), candidate


def publish(lifecycle, candidate):
    identity = candidate["candidate_id"]
    lifecycle.review(identity, Reviewer())
    return lifecycle.publish(identity, lambda view: True)


def test_review_and_explicit_confirmation_publish_then_withdraw_without_rewriting_evidence(
    publication,
):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    path = lifecycle.proposals.directory / (identity + ".json")
    frozen = path.read_bytes()
    assert lifecycle.inspect(identity)["state"]["status"] == "pending"
    assert lifecycle.current() == []

    def unlocked_review(received, opinion):
        with FileLock(str(lifecycle.proposals.lock.lock_file), timeout=0):
            assert received == candidate
        received["proposal"]["statement"] = "改返回副本不能改候选"

    reviewed = lifecycle.review(identity, Reviewer(unlocked_review))
    assert reviewed["state"]["status"] == "reviewed"
    assert reviewed["semantic_status"] == "not_verified"
    assert lifecycle.current() == []
    confirmations = []

    def confirm(view):
        with FileLock(str(lifecycle.proposals.lock.lock_file), timeout=0):
            confirmations.append(copy.deepcopy(view))
        view["state"]["review"]["summary"] = "不能改保存的审查"
        return True

    adopted = lifecycle.publish(identity, confirm)
    assert adopted["state"]["status"] == "published"
    assert adopted["state"]["decision_source"] == "user"
    assert confirmations[0]["candidate"] == candidate
    assert confirmations[0]["state"] == reviewed["state"]
    current = lifecycle.current(now=datetime(2026, 11, 2, tzinfo=UTC))
    assert [v["candidate"] for v in current] == [candidate]
    assert current[0]["source_status"] == "current_snapshot"
    assert current[0]["semantic_status"] == "not_verified"
    assert lifecycle.publish(identity, confirm) == adopted
    assert len(confirmations) == 1
    resumed = KnowledgeLifecycle(lifecycle.proposals)
    withdrawn = resumed.withdraw(identity, "用户不再采用此规则")
    assert resumed.withdraw(identity, "重复撤回") == withdrawn
    assert resumed.current() == []
    assert [e["status"] for e in resumed.history(identity)] == [
        "reviewed",
        "published",
        "withdrawn",
    ]
    assert path.read_bytes() == frozen
    with pytest.raises(DocumentError, match="不能重新审查"):
        resumed.review(identity, Reviewer())


@pytest.mark.parametrize("verdict", [None, "revise", "reject"])
def test_only_accepted_review_can_reach_user_confirmation(publication, verdict):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    if verdict is not None:
        lifecycle.review(identity, Reviewer(verdict=verdict))
        assert lifecycle.inspect(identity)["state"]["status"] == "changes_requested"
    calls = []
    with pytest.raises(DocumentError, match="尚未通过"):
        lifecycle.publish(identity, lambda view: calls.append(view) or True)
    assert calls == [] and lifecycle.current() == []
    if verdict is not None:
        lifecycle.review(identity, Reviewer())
        assert lifecycle.inspect(identity)["state"]["status"] == "reviewed"
        assert len(lifecycle.history(identity)) == 2


@pytest.mark.parametrize("fault", ["approval", "missing_counter", "unknown_id", "verdict", "throw"])
def test_bad_review_never_becomes_publishable(publication, fault):
    lifecycle, candidate = publication

    def malformed(received, opinion):
        if fault == "approval":
            opinion["approved"] = True
        elif fault == "missing_counter":
            opinion["evidence_ids"] = [received["proposal"]["support"][0]["chunk_id"]]
        elif fault == "unknown_id":
            opinion["evidence_ids"] = ["f" * 16]
        elif fault == "verdict":
            opinion["verdict"] = ["accept"]
        else:
            raise RuntimeError("injected remote review failure")

    with pytest.raises(RuntimeError if fault == "throw" else DocumentError):
        lifecycle.review(candidate["candidate_id"], Reviewer(malformed))
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "pending"
    assert not lifecycle.path.exists()


@pytest.mark.parametrize("operation", ["reject", "review", "source"])
def test_review_result_cannot_overwrite_changes_made_while_reviewer_runs(
    publication, tmp_path, operation
):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]

    def competing_action(received, opinion):
        if operation == "reject":
            lifecycle.reject(identity, "用户已决定拒绝")
        elif operation == "review":
            lifecycle.review(identity, Reviewer(verdict="revise"))
        else:
            (tmp_path / "docs/new.md").write_text("新规则改成三次。", encoding="utf-8")

    with pytest.raises(DocumentError):
        lifecycle.review(identity, Reviewer(competing_action))
    expected = {"reject": "rejected", "review": "changes_requested", "source": "pending"}
    assert lifecycle.inspect(identity)["state"]["status"] == expected[operation]
    assert lifecycle.current() == []


@pytest.mark.parametrize("operation", ["reject", "review", "source"])
def test_user_confirmation_is_invalid_after_concurrent_decision_or_source_change(
    publication, tmp_path, operation
):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    lifecycle.review(identity, Reviewer())

    def stale_confirmation(view):
        if operation == "reject":
            lifecycle.reject(identity, "在另一入口拒绝")
        elif operation == "review":
            lifecycle.review(identity, Reviewer(verdict="revise"))
        else:
            (tmp_path / "docs/new.md").write_text("规则已变化。", encoding="utf-8")
        return True

    with pytest.raises(DocumentError):
        lifecycle.publish(identity, stale_confirmation)
    assert all(e["status"] != "published" for e in lifecycle.history(identity))
    assert lifecycle.current() == []


def test_denial_is_terminal_and_non_boolean_confirmation_is_not_approval(publication):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    lifecycle.review(identity, Reviewer())
    with pytest.raises(DocumentError, match="布尔"):
        lifecycle.publish(identity, lambda view: "yes")
    assert lifecycle.inspect(identity)["state"]["status"] == "reviewed"
    denied = lifecycle.publish(identity, lambda view: False)
    assert denied["state"]["status"] == "rejected"
    assert lifecycle.reject(identity, "重复拒绝") == denied
    with pytest.raises(DocumentError):
        lifecycle.publish(identity, lambda view: True)
    assert lifecycle.current() == []


def test_source_change_persists_needs_review_and_old_snapshot_cannot_be_republished(
    publication, tmp_path
):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    publish(lifecycle, candidate)
    path = tmp_path / "docs/new.md"
    previous = path.read_bytes()
    path.write_text("规则已变，原候选须重新审查。", encoding="utf-8")
    assert lifecycle.current() == []
    resumed = KnowledgeLifecycle(lifecycle.proposals)
    assert resumed.inspect(identity)["state"]["status"] == "needs_review"
    assert resumed.history(identity)[-1]["decision_source"] == "source_change"
    path.write_bytes(previous)
    assert resumed.current() == []
    with pytest.raises(DocumentError):
        resumed.publish(identity, lambda view: True)
    assert resumed.withdraw(identity, "撤回失效知识")["state"]["status"] == "withdrawn"
    assert resumed.proposals.read(identity) == candidate


def test_current_facts_respect_time_boundaries_and_do_not_reinterpret_unknown_dates(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    proposal["valid_until"] = "2026-12-01T00:00:00+08:00"
    lifecycle = KnowledgeLifecycle(KnowledgeProposals(tmp_path / "knowledge", index))
    candidate = lifecycle.proposals.create(proposal)
    publish(lifecycle, candidate)
    at = datetime.fromisoformat
    assert lifecycle.current(now=at("2026-10-31T23:59:59+08:00")) == []
    assert len(lifecycle.current(now=at("2026-11-01T00:00:00+08:00"))) == 1
    assert lifecycle.current(now=at("2026-12-01T00:00:00+08:00")) == []
    assert lifecycle.inspect(candidate["candidate_id"])["state"]["status"] == "published"
    with pytest.raises(DocumentError, match="时区"):
        lifecycle.current(now=datetime(2026, 11, 2))


def test_permission_loss_stops_current_query_but_does_not_prevent_user_withdrawal(publication):
    lifecycle, candidate = publication
    publish(lifecycle, candidate)

    def revoke(path):
        raise ToolError("injected source permission loss")

    lifecycle.proposals.index.source.authorize = revoke
    with pytest.raises(ToolError, match="permission loss"):
        lifecycle.current()
    assert (
        lifecycle.withdraw(candidate["candidate_id"], "来源已不可访问")["state"]["status"]
        == "withdrawn"
    )


@pytest.mark.parametrize("operation", ["review", "publish", "withdraw", "invalidate"])
def test_atomic_state_save_failure_preserves_last_committed_decision(
    publication, monkeypatch, tmp_path, operation
):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    if operation in {"publish", "withdraw", "invalidate"}:
        lifecycle.review(identity, Reviewer())
    if operation in {"withdraw", "invalidate"}:
        lifecycle.publish(identity, lambda view: True)
    before = lifecycle.path.read_bytes() if lifecycle.path.exists() else None
    if operation == "invalidate":
        (tmp_path / "docs/new.md").write_text("新规则改成三次。", encoding="utf-8")
        lifecycle.proposals.index.refresh()

    def deny_replace(*args):
        raise PermissionError("injected state replacement failure")

    monkeypatch.setattr("adam.documents.snapshots.os.replace", deny_replace)
    actions = {
        "review": lambda: lifecycle.review(identity, Reviewer()),
        "publish": lambda: lifecycle.publish(identity, lambda view: True),
        "withdraw": lambda: lifecycle.withdraw(identity, "用户撤回"),
        "invalidate": lifecycle.current,
    }
    with pytest.raises(DocumentError, match="读写失败"):
        actions[operation]()
    assert (lifecycle.path.read_bytes() if lifecycle.path.exists() else None) == before
    assert not list(lifecycle.path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "fault", ["truncated", "skip_review", "replace_review", "candidate_changed"]
)
def test_corrupt_or_inconsistent_history_is_preserved_and_never_reused(publication, fault):
    lifecycle, candidate = publication
    identity = candidate["candidate_id"]
    publish(lifecycle, candidate)
    if fault == "truncated":
        lifecycle.path.write_text('{"version":', encoding="utf-8")
    elif fault == "candidate_changed":
        path = lifecycle.proposals.directory / (identity + ".json")
        payload = load_snapshot(path)
        payload["proposal"]["statement"] = "候选被外部替换"
        payload["content_hash"] = digest({k: v for k, v in payload.items() if k != "content_hash"})
        save_snapshot(path, payload)
    else:
        ledger = load_snapshot(lifecycle.path)
        if fault == "skip_review":
            ledger["events"] = ledger["events"][1:]
            ledger["events"][0]["revision"] = 1
        else:
            ledger["events"][-1]["review"]["summary"] = "批准时换了一份意见"
        save_snapshot(lifecycle.path, ledger)
    damaged = lifecycle.path.read_bytes()
    with pytest.raises(DocumentError):
        KnowledgeLifecycle(lifecycle.proposals).current()
    assert lifecycle.path.read_bytes() == damaged

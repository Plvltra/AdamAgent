"""B12.1：冲突资料形成待审候选，原文定位、时间、冻结证据与保存失败边界。"""

import copy
from pathlib import Path

import pytest

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.documents.evidence_audit import quote_chunk
from adam.documents.index import DocumentIndex
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.models import DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.tool_calls.policy import PathPolicy


def proposal_fixture(root):
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "old.md").write_text("# 重试规则\n\n旧方案要求无限重试。\n", encoding="utf-8")
    (docs / "new.md").write_text(
        "# 重试规则\n\n从 2026 年 11 月开始，最多重试两次。\n", encoding="utf-8"
    )
    index = DocumentIndex(
        root / "cache",
        root,
        DocumentConfig(roots=(Path("docs"),)),
        PathPolicy(root, PermissionConfig()).resolve,
    )
    corpus = index.refresh()
    current = next(c for c in corpus.chunks if "两次" in c.text)
    old = next(c for c in corpus.chunks if "无限" in c.text)
    proposal = {
        "statement": "当前重试上限为两次。",
        "applicability": "仅适用于本项目 2026 年 11 月开始的新版本。",
        "reasoning": "新旧方案冲突；候选认为新方案有明确开始时间，但仍需独立审查。",
        "valid_from": "2026-11-01T00:00:00+08:00",
        "valid_until": None,
        "support": [quote_chunk(current)],
        "counterevidence": [quote_chunk(old)],
        "source_generation": corpus.generation,
    }
    return index, proposal


def test_conflicting_sources_become_pending_candidate_with_exact_time_and_evidence(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    original = {p: p.read_bytes() for p in (tmp_path / "docs").iterdir()}
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    candidate = store.create(proposal)
    assert candidate["status"] == "pending" and candidate["semantic_status"] == "unreviewed"
    assert candidate["proposal"] == proposal
    assert candidate["proposal"]["support"][0]["start_line"] == 3
    assert "无限" in candidate["proposal"]["counterevidence"][0]["quote"]
    assert candidate["proposal"]["valid_from"].endswith("+08:00")
    assert candidate["proposal"]["valid_until"] is None
    assert len(candidate["candidate_id"]) == 8
    assert (
        KnowledgeProposals(tmp_path / "knowledge", index).read(candidate["candidate_id"])
        == candidate
    )
    assert {p: p.read_bytes() for p in original} == original
    assert len(list((tmp_path / "knowledge").rglob("*.json"))) == 1
    assert not list((tmp_path / "knowledge").rglob("*published*"))


def test_returned_values_and_input_changes_cannot_rewrite_saved_candidate(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    saved = store.create(proposal)
    expected = copy.deepcopy(saved)
    proposal["support"][0]["quote"] = "changed input"
    saved["proposal"]["statement"] = "changed return"
    assert store.read(saved["candidate_id"]) == expected


def test_old_candidate_keeps_original_evidence_after_sources_change(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    saved = store.create(proposal)
    (tmp_path / "docs/new.md").write_text("新规则现在改成三次。", encoding="utf-8")
    frozen = store.read(saved["candidate_id"])
    assert frozen == saved
    with pytest.raises(DocumentError, match="已过时"):
        store.create(proposal)
    assert len(list(store.directory.glob("*.json"))) == 1


def test_counterevidence_can_be_explicitly_empty_but_support_is_required(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    proposal["counterevidence"] = []
    candidate = store.create(proposal)
    assert candidate["proposal"]["counterevidence"] == []
    proposal["support"] = []
    with pytest.raises(DocumentError, match="支持证据"):
        store.create(proposal)


@pytest.mark.parametrize(
    "fault", ["approval", "timezone", "interval", "mixed_role", "missing_counter", "oversize"]
)
def test_invalid_proposal_does_not_create_candidate_directory(tmp_path, fault):
    index, proposal = proposal_fixture(tmp_path)
    if fault == "approval":
        proposal["approved"] = True
    elif fault == "timezone":
        proposal["valid_from"] = "2026-11-01"
    elif fault == "interval":
        proposal["valid_until"] = "2026-10-01T00:00:00Z"
    elif fault == "mixed_role":
        proposal["counterevidence"] = proposal["support"]
    elif fault == "missing_counter":
        del proposal["counterevidence"]
    else:
        proposal["statement"] = "字" * 2001
    root = tmp_path / "knowledge"
    with pytest.raises(DocumentError):
        KnowledgeProposals(root, index).create(proposal)
    assert not root.exists()


@pytest.mark.parametrize("role", ["support", "counterevidence"])
def test_each_evidence_role_is_checked_against_current_original(tmp_path, role):
    index, proposal = proposal_fixture(tmp_path)
    proposal[role][0]["quote"] = "伪造的原文，不应保存"
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    with pytest.raises(DocumentError, match="错绑"):
        store.create(proposal)
    assert not list(store.directory.glob("*.json"))


def test_same_root_keeps_workspaces_isolated(tmp_path):
    first_index, proposal = proposal_fixture(tmp_path / "first")
    second_index, _ = proposal_fixture(tmp_path / "second")
    root = tmp_path / "knowledge"
    saved = KnowledgeProposals(root, first_index).create(proposal)
    other = KnowledgeProposals(root, second_index)
    with pytest.raises(DocumentError, match="不存在"):
        other.read(saved["candidate_id"])
    with pytest.raises(DocumentError, match="已过时"):
        other.create(proposal)


@pytest.mark.parametrize("fault", ["save", "permission", "cancel"])
def test_failure_before_save_preserves_previous_candidates_and_never_publishes(
    tmp_path, monkeypatch, fault
):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    saved = store.create(proposal)
    before = {p: p.read_bytes() for p in store.directory.iterdir()}
    expected_error = DocumentError
    if fault == "save":

        def deny(*args):
            raise PermissionError("injected candidate atomic replacement failure")

        monkeypatch.setattr("adam.documents.snapshots.os.replace", deny)
    elif fault == "permission":

        def revoke(path):
            raise ToolError("injected source permission withdrawal")

        index.source.authorize = revoke
        expected_error = ToolError
    else:
        token = CancellationToken()
        token.cancel()
        index.check_cancel = token.check
        expected_error = RequestInterrupted
    with pytest.raises(expected_error):
        store.create(proposal)
    assert {p: p.read_bytes() for p in store.directory.iterdir()} == before
    assert (store.directory / (saved["candidate_id"] + ".json")).exists()


def test_colliding_id_never_overwrites_previous_candidate(tmp_path, monkeypatch):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    saved = store.create(proposal)
    monkeypatch.setattr(
        "adam.documents.knowledge_proposals.secrets.token_hex", lambda _: saved["candidate_id"]
    )
    with pytest.raises(DocumentError, match="冲突"):
        store.create(proposal)
    assert store.read(saved["candidate_id"]) == saved


def test_forged_published_state_is_rejected_even_with_recalculated_checksums(tmp_path):
    index, proposal = proposal_fixture(tmp_path)
    store = KnowledgeProposals(tmp_path / "knowledge", index)
    saved = store.create(proposal)
    path = store.directory / (saved["candidate_id"] + ".json")
    payload = load_snapshot(path)
    payload["status"] = "published"
    payload["content_hash"] = digest({k: v for k, v in payload.items() if k != "content_hash"})
    save_snapshot(path, payload)
    before = path.read_bytes()
    with pytest.raises(DocumentError, match="保留原文件"):
        store.read(saved["candidate_id"])
    assert path.read_bytes() == before
    with pytest.raises(DocumentError, match="8 位"):
        store.read("../other")

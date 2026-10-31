"""提案输入与持久载荷的协议边界；来源与文件行为在 features 验收。"""

from dataclasses import asdict

import pytest

from adam.contracts.proposals import (
    MAX_PATCH_BYTES,
    EditProposal,
    ProposalEdit,
    ProposalOwner,
    ReadEvidence,
    decode_proposal,
    decode_submission,
)


def submission(**changes):
    return {
        "read_call_id": "read",
        "old_text": "before",
        "new_text": "after",
        "reason": "fix",
        **changes,
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        submission(path="target.py"),
        submission(expected_version="a" * 64),
        submission(parent_session_id="aaaaaaaa"),
        submission(read_call_id=""),
        submission(old_text=""),
        submission(old_text="same", new_text="same"),
        submission(new_text="x\0y"),
        submission(old_text="x\ry"),
        submission(new_text="汉" * (MAX_PATCH_BYTES // 3 + 1)),
        submission(reason=" "),
        submission(reason="x" * 501),
        submission(new_text=1),
    ],
)
def test_model_cannot_add_identity_fields_or_unbounded_invalid_patch(value):
    with pytest.raises(ValueError):
        decode_submission(value)


def proposal_at(root):
    return EditProposal(
        "a" * 16,
        ProposalOwner("aaaaaaaa", "b" * 32, "cccccccc", "d" * 16),
        "e" * 32,
        "f" * 32,
        "submit",
        ReadEvidence("1" * 32, "read", str(root / "file.py"), "2" * 64, 1, 2),
        ProposalEdit("read", "before", "after", "fix"),
        "3" * 64,
    )


def test_serialized_copy_does_not_mutate_saved_candidate(tmp_path):
    proposal = proposal_at(tmp_path)
    value = proposal.to_dict()
    assert decode_proposal(value) == proposal
    value["edit"]["new_text"] = "mutated"
    value["owner"]["run_id"] = "4" * 16
    assert proposal.edit.new_text == "after" and proposal.owner.run_id == "d" * 16


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal_id", "invalid"),
        ("target_version", "short"),
        ("turn_id", True),
        ("read", {}),
        ("owner", None),
        ("edit", {"fake": 1}),
    ],
)
def test_persisted_contract_rejects_missing_or_wrong_identity(tmp_path, field, value):
    payload = asdict(proposal_at(tmp_path))
    payload[field] = value
    with pytest.raises(ValueError):
        decode_proposal(payload)


def test_persisted_patch_must_already_be_canonical(tmp_path):
    payload = proposal_at(tmp_path).to_dict()
    payload["edit"]["old_text"] = "first\r\nsecond"
    with pytest.raises(ValueError):
        decode_proposal(payload)
    assert decode_submission(submission(old_text="first\r\nsecond")).old_text == "first\nsecond"

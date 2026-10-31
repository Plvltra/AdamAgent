"""子会话中的不可变提案投影；保存候选不代表文件已被修改。"""

import json
from typing import Any

from adam.contracts.proposals import (
    MAX_PROPOSALS_PER_TURN,
    EditProposal,
    decode_proposal,
    decode_submission,
    validate_proposal_read,
)


class ProposalLog:
    """来源和补丁均与同轮实际工具核对；读取历史不重新执行工具或检查当前源文件。"""

    def __init__(self) -> None:
        self.records: dict[str, EditProposal] = {}

    def apply(
        self,
        payload: dict[str, Any],
        session_id: str,
        tools: dict[tuple[str, str], dict[str, Any]],
        submitting: dict[str, Any],
    ) -> None:
        """校验唯一 ID、派发范围、调用参数和已读正文，全部通过才更新投影。"""
        proposal = decode_proposal(payload)
        if proposal.owner.child_session_id != session_id:
            raise ValueError("proposal belongs to another child session")
        current = [p for p in self.records.values() if p.turn_id == proposal.turn_id]
        if (
            proposal.proposal_id in self.records
            or any(p.tool_call_id == proposal.tool_call_id for p in current)
            or len(current) >= MAX_PROPOSALS_PER_TURN
        ):
            raise ValueError("duplicate proposal or child turn proposal limit reached")
        if any(p.owner != proposal.owner for p in current):
            raise ValueError("proposal dispatch changed within a child turn")
        if decode_submission(json.loads(submitting["arguments"])) != proposal.edit:
            raise ValueError("proposal differs from its actual submit arguments")
        validate_proposal_read(proposal, tools)
        self.records[proposal.proposal_id] = proposal

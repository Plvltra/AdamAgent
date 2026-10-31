"""主任务从实际子日志收集候选，经原审批和检查点流程应用，不接收模型提供的补丁。"""

import json
from hashlib import sha256
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.models import EditPlan, ExecutionRecorder
from adam.contracts.proposals import EditProposal, ProposalSource
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools


class ProposalReview:
    """只保存已有编辑事实；候选来源仍由所属子会话管理，不另建可漂移的应用状态。"""

    def __init__(
        self, store: SessionStore, source: ProposalSource, edits: EditTools, max_result_chars: int
    ) -> None:
        self.store, self.source, self.edits = store, source, edits
        self.max_result_chars = max_result_chars

    def collect(
        self, agent_id: str, run_id: str, index: int = 0, offset: int = 0
    ) -> dict[str, Any]:
        """按候选序号和字符偏移返回正文片段；每页的完整 JSON 保留路径、ID 和续读位置。"""
        proposals = self.source.candidates(agent_id, run_id)
        if type(index) is not int or type(offset) is not int or index < 0 or offset < 0:
            raise ToolError("提案序号和偏移须为非负整数。")
        if not proposals and index == offset == 0:
            return {"status": "empty", "agent_id": agent_id, "run_id": run_id, "total": 0}
        if index >= len(proposals):
            raise ToolError("提案序号超出范围。")
        proposal = proposals[index]
        text = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2)
        if offset >= len(text):
            raise ToolError("提案偏移超出范围。")
        metadata = {
            "status": "collected",
            "agent_id": agent_id,
            "run_id": run_id,
            "proposal_id": proposal.proposal_id,
            "path": proposal.read.path,
            "index": index,
            "total": len(proposals),
            "offset": offset,
            "next_index": index + 1 if index + 1 < len(proposals) else None,
        }
        return self._page(metadata, text, offset)

    def validate_apply(self, value: object) -> None:
        """在规则及 Hook 前核对参数；path 只供审计，必须原样匹配真实候选路径。"""
        if (
            not isinstance(value, dict)
            or set(value) != {"agent_id", "run_id", "proposal_id", "path"}
            or any(not isinstance(item, str) or not item for item in value.values())
        ):
            raise ToolError("应用提案须提供 agent_id、run_id、proposal_id 和候选的完整 path。")
        self._get(value["agent_id"], value["run_id"], value["proposal_id"], value["path"])

    def apply(
        self,
        agent_id: str,
        run_id: str,
        proposal_id: str,
        path: str,
        record: ExecutionRecorder | None,
    ) -> dict[str, Any]:
        """重新核对来源、当前权限及版本；批准后才通过原编辑流程写入并保存检查点。

        path 是审计确认值，不能改变目标；旧/新文本和版本仅取自真实候选。
        record 必须由工具执行器提供，缺失时拒绝；日志故障保持原停止语义。
        已应用或结果未知的候选不能重复应用，即使文件后来被撤销回旧版本。
        """
        if record is None:
            raise ToolError("应用提案必须通过带持久化记录的工具执行器。")
        proposal = self._get(agent_id, run_id, proposal_id, path)
        self._check_unused(proposal)
        plan = self._prepare(proposal)

        def record_proposal(kind: str, payload: dict[str, Any]) -> None:
            # 完整候选随原编辑计划保存；后续成功/未知状态只取原编辑事件。
            if kind == "edit_planned":
                payload = {**payload, "proposal": proposal.to_dict()}
            record(kind, payload)

        return self.edits.execute_plan(plan, record_proposal)

    def _get(
        self, agent_id: str, run_id: str, proposal_id: str, path: str | None = None
    ) -> EditProposal:
        """每次从已完成的真实派发获取，不信任模型回复或跨会话缓存。"""
        for proposal in self.source.candidates(agent_id, run_id):
            if proposal.proposal_id == proposal_id:
                if path is not None and path != proposal.read.path:
                    raise ToolError(
                        "应用路径与真实提案不符；请复制 collect_proposals 返回的 path。"
                    )
                return proposal
        raise ToolError("提案不存在或不属于指定子任务。")

    def _check_unused(self, proposal: EditProposal) -> None:
        """从实际编辑状态拒绝重复副作用；撤销后也不会重新开放旧候选。"""
        owner = proposal.to_dict()["owner"]
        for edit in self.store.load_edits(proposal.owner.parent_session_id):
            saved = edit.get("proposal", {})
            if (
                saved.get("proposal_id") == proposal.proposal_id
                and saved.get("owner") == owner
                and edit["status"] in {"applied", "unknown"}
            ):
                raise ToolError("该提案已经应用或结果未知；不能重复执行，请检查原编辑记录。")

    def _prepare(self, proposal: EditProposal) -> EditPlan:
        """重做当前写入权限、原版本和唯一替换校验，再核对候选承诺的目标字节。"""
        plan = self.edits.prepare(
            {
                "path": proposal.read.path,
                "old_text": proposal.edit.old_text,
                "new_text": proposal.edit.new_text,
                "expected_version": proposal.read.version,
            }
        )
        if str(plan.path) != proposal.read.path or sha256(plan.candidate).hexdigest() != (
            proposal.target_version
        ):
            raise ToolError("提案路径或目标版本校验失败；未申请批准、未修改文件。")
        return plan

    def _page(self, metadata: dict[str, Any], text: str, offset: int) -> dict[str, Any]:
        """按实际 JSON 长度选取字符窗口；无法容纳身份元数据时明确失败，不截掉 ID。"""

        def page(count: int) -> dict[str, Any]:
            end = offset + count
            return {
                **metadata,
                "text": text[offset:end],
                "next_offset": end if end < len(text) else None,
            }

        low, high = 0, min(len(text) - offset, self.max_result_chars)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps({"ok": True, **page(middle)}, ensure_ascii=False)) <= (
                self.max_result_chars
            ):
                low = middle
            else:
                high = middle - 1
        if low == 0:
            raise ToolError("结果上限不足以容纳提案身份和路径；请增大工具结果上限。")
        return page(low)

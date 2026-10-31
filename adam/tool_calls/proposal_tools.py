"""子任务提交及主任务收集/应用工具；仅依赖共享接口，不依赖应用实现。"""

from pathlib import Path
from typing import Any

from adam.contracts.execution import ToolCapability
from adam.contracts.proposals import ProposalReviewer, ProposalSubmitter
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

SUBMIT_SPEC = {
    "submit_proposal": schema(
        "提出一个文件的一处唯一替换，不修改文件。必须引用本轮真实 read_file，"
        "旧文本须完整出现在该次读取窗口；路径和版本由程序取自读取记录。主 Agent 负责批准应用。",
        {
            "read_call_id": ("string", "本子轮已完成的 read_file 调用 ID"),
            "old_text": ("string", "实际读到且在全文件唯一的旧文本"),
            "new_text": ("string", "替换文本；空字符串表示删除"),
            "reason": ("string", "修改理由，最多500字符"),
        },
        ["read_call_id", "old_text", "new_text", "reason"],
    )
}
PARENT_SPECS = {
    "collect_proposals": schema(
        "先 wait_agents 收集子结果，再读取已完成辅助子任务的真实提案。index选择候选，"
        "offset按字符分页；跟随next_offset读完正文，再用next_index查看下一份。没有应用文件。",
        {
            "agent_id": ("string", "本主会话的辅助子 Agent ID"),
            "run_id": ("string", "已完成的派发 ID"),
            "index": ("integer", "候选序号，默认0"),
            "offset": ("integer", "正文字符偏移，默认0"),
        },
        ["agent_id", "run_id"],
    ),
    "apply_proposal": schema(
        "审核子任务提案后申请用户批准并应用；复用编辑额度和检查点。"
        "仅接收来源身份与审计路径，补丁与版本取自真实子日志；path须原样复制collect_proposals结果供Hook审计。"
        "文件变化或提案已应用时拒绝，不自动重试。",
        {
            "agent_id": ("string", "本主会话的辅助子 Agent ID"),
            "run_id": ("string", "已完成的派发 ID"),
            "proposal_id": ("string", "提案 ID"),
            "path": ("string", "collect_proposals返回的完整path，仅核对，不用于选择其他文件"),
        },
        ["agent_id", "run_id", "proposal_id", "path"],
    ),
}


def register_submission(registry: ToolRegistry, service: ProposalSubmitter) -> ToolRegistry:
    """仅为辅助子任务注册内部候选记录，不增加编辑或命令权限。"""

    def submit(args: dict[str, Any], _: object) -> dict[str, Any]:
        proposal = service.submit(**args)
        return {"status": "submitted", "proposal_id": proposal.proposal_id}

    return registry.extended(
        SUBMIT_SPEC,
        {"submit_proposal": submit},
        capabilities={"submit_proposal": ToolCapability.SESSION_STATE},
    )


def register_review(registry: ToolRegistry, service: ProposalReviewer) -> ToolRegistry:
    """主任务应用必须先验证审计路径，再经过模式、规则、Hook 与原编辑批准。"""
    return registry.extended(
        PARENT_SPECS,
        {
            "collect_proposals": lambda args, _: service.collect(**args),
            "apply_proposal": lambda args, record: service.apply(**args, record=record),
        },
        validators={"apply_proposal": service.validate_apply},
        capabilities={
            "collect_proposals": ToolCapability.SESSION_STATE,
            "apply_proposal": ToolCapability.EDIT_FILE,
        },
        path_selectors={"apply_proposal": lambda args: (Path(args["path"]),)},
    )

"""计划证据只读取已落盘的工具事实；不能用模型提供的输出冒充执行记录。"""

import json
from typing import Any

from adam.contracts.planning import PlanSnapshot


def validate_evidence(
    plan: PlanSnapshot,
    tools: dict[tuple[str, str], dict[str, Any]],
    executions: dict[str, dict[str, Any]],
    edits: dict[str, dict[str, Any]],
) -> None:
    """校验同轮已结束工具；完成步骤还要求成功，失败步骤可引用真实失败证据。"""
    for step in plan.steps:
        for call_id in step.evidence:
            tool = tools.get((plan.turn_id, call_id))
            if tool is None or not tool["finished"] or tool["name"] == "update_plan":
                raise ValueError("证据须引用本轮已结束的实际工作工具，不能引用计划工具")
            if step.status == "completed" and not successful(tool, executions, edits):
                raise ValueError("完成步骤的证据必须成功；命令要求退出码 0，编辑要求 applied")


def successful(
    tool: dict[str, Any],
    executions: dict[str, dict[str, Any]],
    edits: dict[str, dict[str, Any]],
) -> bool:
    """区分工具返回与实际副作用；无法确认的结果不作为完成证据。"""
    output = json.loads(tool["output"])
    if not isinstance(output, dict) or output.get("ok") is not True:
        return False
    if tool["name"] == "run_command":
        execution = executions.get(tool.get("execution_id") or "", {})
        return execution.get("status") == "completed" and execution.get("exit_code") == 0
    if tool["name"] == "replace_text":
        return edits.get(tool.get("edit_id") or "", {}).get("status") == "applied"
    if tool["name"] == "wait_agents":
        agents = output.get("agents")
        return (
            isinstance(agents, list)
            and bool(agents)
            and all(
                isinstance(agent, dict) and agent.get("status") == "completed" for agent in agents
            )
        )
    return output.get("status") not in {
        "denied",
        "approval_required",
        "failed",
        "interrupted",
        "unknown",
        "running",
        "stopping",
    }

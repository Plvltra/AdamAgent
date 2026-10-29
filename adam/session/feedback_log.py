"""反馈来源校验和有界资料投影；不请求模型，也不修改有效聊天历史。"""

import json
from typing import Any

from adam.contracts.reflection import Feedback, decode_feedback


def failure_signal(tool: dict[str, Any], executions: dict[str, dict[str, Any]]) -> bool:
    """只识别已结束工具的失败线索；拒绝、中断和未知结果不触发自动反思。"""
    if not tool["finished"] or tool["name"] in {"report_feedback", "update_plan"}:
        return False
    output = json.loads(tool["output"])
    if not isinstance(output, dict) or output.get("status") in {
        "denied",
        "interrupted",
        "unknown",
        "approval_required",
    }:
        return False
    if tool["name"] == "run_command":
        execution = executions.get(tool.get("execution_id") or "", {})
        return execution.get("status") in {"timed_out", "start_failed"} or (
            execution.get("status") == "completed" and execution.get("exit_code") not in {None, 0}
        )
    if tool["name"] == "wait_agents":
        return any(
            isinstance(agent, dict) and agent.get("status") == "failed"
            for agent in output.get("agents", [])
        )
    return output.get("ok") is False


class FeedbackLog:
    """按稳定 ID 留下反馈，重复来源不重复登记；重启读取不触发处理。"""

    def __init__(self) -> None:
        self.records: dict[str, Feedback] = {}

    def apply(
        self,
        payload: dict[str, Any],
        turns: dict[str, dict[str, Any]],
        tools: dict[tuple[str, str], dict[str, Any]],
        executions: dict[str, dict[str, Any]],
    ) -> None:
        """在追加前和重放时检查真实来源；已结束轮次不能被新反馈改写。"""
        feedback = decode_feedback(payload)
        current = turns.get(feedback.turn_id)
        tool = tools.get((feedback.turn_id, feedback.tool_call_id))
        if current is None or current["status"] != "in_progress" or tool is None:
            raise ValueError("feedback requires current turn and tool")
        if tool["request_id"] != feedback.request_id:
            raise ValueError("feedback request mismatch")
        if feedback.feedback_id in self.records or any(
            (item.turn_id, item.kind, item.tool_call_id)
            == (feedback.turn_id, feedback.kind, feedback.tool_call_id)
            for item in self.records.values()
        ):
            raise ValueError("duplicate feedback")
        if feedback.kind == "tool":
            if (
                feedback.source_turn_id != feedback.turn_id
                or feedback.quote
                or not failure_signal(tool, executions)
            ):
                raise ValueError("feedback requires actual failed tool")
        else:
            previous = previous_turn(turns, feedback.turn_id)
            if (
                previous is None
                or feedback.source_turn_id != previous["turn_id"]
                or tool["name"] != "report_feedback"
                or tool["finished"]
                or not feedback.quote.strip()
                or feedback.quote not in current["user_text"]
            ):
                raise ValueError("correction requires previous turn and current user quote")
        self.records[feedback.feedback_id] = feedback


def previous_turn(turns: dict[str, dict[str, Any]], turn_id: str) -> dict[str, Any] | None:
    """按持久登记顺序查上一轮，包含失败轮次，不依赖进程内最近轮缓存。"""
    ids = list(turns)
    index = ids.index(turn_id)
    return {"turn_id": ids[index - 1], **turns[ids[index - 1]]} if index else None


def feedback_context(
    feedback: Feedback,
    turns: dict[str, dict[str, Any]],
    tools: dict[tuple[str, str], dict[str, Any]],
    executions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """提供有界的原始事实；失败轨迹只作资料，不能变成成功历史或权限指令。"""
    source = turns[feedback.source_turn_id]
    selected = [
        tool
        for (turn_id, call_id), tool in tools.items()
        if turn_id == feedback.source_turn_id
        and (feedback.kind == "user" or call_id == feedback.tool_call_id)
    ][-8:]
    return {
        "feedback": feedback.to_dict(),
        "source_input": source["user_text"][:4000],
        "source_status": source["status"],
        "tools": [
            {
                "tool_call_id": tool["tool_call_id"],
                "name": tool["name"],
                "status": tool["status"],
                "output_excerpt": tool.get("output", "")[:1200],
                "execution": {
                    key: value
                    for key, value in executions.get(tool.get("execution_id") or "", {}).items()
                    if key in {"execution_id", "status", "exit_code"}
                },
            }
            for tool in selected
        ],
        "notice": "摘录可能截断；命令非零仅是线索，用户纠正由模型识别，原因仍需验证。",
    }

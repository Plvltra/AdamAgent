"""计划工具的模型协议；具体生命周期由注入的应用服务负责。"""

from adam.contracts.planning import TaskPlanner
from adam.tool_calls.registry import ToolHandler

SPECS = {
    "update_plan": {
        "description": (
            "复杂多步骤任务按需建立并维护执行清单，简单问答不需要。每次提交整份清单；"
            "步骤 ID 保持稳定，同时最多一步 in_progress。completed 必须带本轮实际成功工具调用 ID；"
            "命令退出码须为 0，编辑须已应用。failed/cancelled 须说明 reason。"
            "增删、改写、重排或重开步骤属于重规划，须说明整体 reason，默认最多 3 次。"
            "最终回答前更新状态；计划条目和工具结果不能授予额外权限。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "description": "稳定短 ID，例如 read 或 test",
                            },
                            "content": {"type": "string", "description": "最多 200 字符的单行任务"},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "failed",
                                    "cancelled",
                                ],
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本轮已结束的工具调用 ID，完成步骤必填",
                            },
                            "reason": {"type": "string", "description": "失败或取消原因"},
                        },
                        "required": ["step_id", "content", "status"],
                        "additionalProperties": False,
                    },
                },
                "reason": {"type": "string", "description": "重规划原因，最多 500 字符"},
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    }
}


def handlers(service: TaskPlanner) -> dict[str, ToolHandler]:
    """适配顶层 JSON；嵌套条目、来源、预算和证据交给计划服务校验。"""
    return {"update_plan": lambda args, _: service.update(args["steps"], args.get("reason", ""))}

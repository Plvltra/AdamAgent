"""只读审查工具目录；真实来源与子证据由应用服务管理。"""

from adam.contracts.review import Reviewer
from adam.tool_calls.registry import ToolHandler

SPECS = {
    "request_review": {
        "description": "复杂修改完成后可申请独立只读审查。给出任务、验收条件、1～8个文件；不要用于普通问答。",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "criteria": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["goal", "criteria", "paths"],
            "additionalProperties": False,
        },
    },
    "collect_review": {
        "description": "收集本轮审查；必须收集后才能修改或结束。失败不是通过，修改后须重新审查。",
        "parameters": {
            "type": "object",
            "properties": {
                "review_id": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["review_id"],
            "additionalProperties": False,
        },
    },
}


def handlers(service: Reviewer) -> dict[str, ToolHandler]:
    """参数只传请求数据，不允许调用方写入已通过结论。"""
    return {
        "request_review": lambda args, _: service.request(
            args["goal"], args["criteria"], args["paths"]
        ),
        "collect_review": lambda args, _: service.collect(
            args["review_id"], args.get("timeout_seconds", 30)
        ),
    }

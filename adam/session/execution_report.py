"""从已校验日志提取本轮执行事实，不采信模型对修改或测试结果的自述。"""

import json

from adam.contracts.models import ActionFact, ExecutionReport
from adam.session.log import SessionLog


def build_execution_report(log: SessionLog, turn_id: str) -> ExecutionReport:
    """读取指定轮次的工具和副作用记录，保持执行顺序；不执行 IO 或恢复操作。

    缺少副作用开始记录表示尚未开始；有开始但无结束的记录保持 unknown。
    版本和退出码仅取自独立结果记录，不能用最终回答或工具 ok 字段推断测试通过。
    """
    turn = log.turns[turn_id]
    tools = [item for item in log.tools.values() if item["turn_id"] == turn_id]
    actions = []
    for tool in tools:
        name = tool["name"]
        if name not in {"run_command", "replace_text"}:
            continue
        if name == "run_command":
            record = log.executions.get(tool["execution_id"])
        else:
            edit_id = tool.get("edit_id")
            record = log.edits.operations.get(edit_id) if edit_id is not None else None
        if record is None:
            actions.append(
                ActionFact(
                    tool["tool_call_id"],
                    name,
                    "未生成可执行计划",
                    "not_started",
                )
            )
            continue
        target = (
            json.dumps({"argv": record["argv"], "cwd": record["cwd"]}, ensure_ascii=False)
            if name == "run_command"
            else json.dumps(record["path"], ensure_ascii=False)
        )
        actions.append(
            ActionFact(
                tool["tool_call_id"],
                name,
                target,
                "not_started" if record["status"] == "planned" else record["status"],
                record.get("exit_code"),
                record.get("before_version"),
                record.get("after_version"),
            )
        )
    return ExecutionReport(
        turn_id,
        turn["status"],
        turn.get("reason"),
        bool(tools),
        tuple(actions),
    )

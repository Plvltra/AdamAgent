"""从已校验日志提取本轮执行事实，不采信模型对修改或测试结果的自述。"""

import json
from dataclasses import dataclass

from adam.session_log import SessionLog


@dataclass(frozen=True)
class ActionFact:
    """一次实际派发的副作用工具；目标已用 JSON 转义，避免换行或控制符伪造报告。"""

    tool_call_id: str
    kind: str
    target: str
    status: str
    exit_code: int | None = None
    before_version: str | None = None
    after_version: str | None = None


@dataclass(frozen=True)
class ExecutionReport:
    """轮次执行快照；成功状态只表示回答提交，不承诺任务或测试正确。"""

    turn_id: str
    status: str
    reason: str | None
    has_tools: bool
    actions: tuple[ActionFact, ...]


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

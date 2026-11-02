"""Hook 审计的关联与状态校验；不执行程序，不把观察结果当作工具结果。"""

import json
from typing import Any

from adam.contracts.diagnostics import match_edit, parse_diagnostics
from adam.contracts.execution import ExecutionMode
from adam.contracts.hooks import HookEvent

_LINK = {"turn_id", "request_id", "tool_call_id", "hook_id", "event", "execution_mode"}
_FAILURES = {
    "invalid_input",
    "start_failed",
    "output_limit",
    "io_failed",
    "timed_out",
    "nonzero_exit",
    "invalid_response",
}


class HookLog:
    """保存每个工具边界的一次检查；恢复只关闭遗留状态，不重跑本地程序。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def apply(self, kind: str, payload: dict[str, Any], tool: dict[str, Any]) -> None:
        """检查来源、前后阶段和单次结算；诊断须对应已保存的编辑路径和版本。

        父轮次、请求由 SessionLog 先校验；重启读取只核对记录，不读取文件或重做检查。
        """
        expected = _LINK if kind == "hook_started" else _LINK | {"result"}
        if payload.keys() != expected or any(
            not isinstance(payload.get(key), str) or not payload[key] for key in _LINK
        ):
            raise ValueError("invalid hook record fields")
        event = HookEvent(payload["event"])
        ExecutionMode(payload["execution_mode"])
        if tool["finished"] != (event is HookEvent.AFTER_TOOL):
            raise ValueError("hook boundary does not match tool state")
        identifier = payload["hook_id"]
        if kind == "hook_started":
            same_tool = [
                item
                for item in self.records.values()
                if (item["turn_id"], item["tool_call_id"])
                == (payload["turn_id"], payload["tool_call_id"])
            ]
            if identifier in self.records or any(
                item["event"] == event.value for item in same_tool
            ):
                raise ValueError("duplicate hook boundary")
            if event is HookEvent.AFTER_TOOL and any(
                item["status"] != "allowed" for item in same_tool
            ):
                raise ValueError("after hook requires allowed before hook")
            self.records[identifier] = {**payload, "status": "in_progress"}
            return
        previous = self.records.get(identifier)
        if (
            previous is None
            or previous["status"] != "in_progress"
            or any(payload[key] != previous[key] for key in _LINK)
        ):
            raise ValueError("unknown, duplicate or mismatched hook finish")
        result = payload["result"]
        _validate_result(result, event)
        if "diagnostics" in result:
            match_edit(
                parse_diagnostics(result["diagnostics"]), tool["name"], json.loads(tool["output"])
            )
        previous.update(result)

    def check_tool_finish(self, tool: dict[str, Any], output: str) -> None:
        """前置未结算不能结束工具；被阻止的动作不能伪装为执行成功。"""
        for item in self.records.values():
            if (item["turn_id"], item["tool_call_id"]) == (
                tool["turn_id"],
                tool["tool_call_id"],
            ) and item["event"] == HookEvent.BEFORE_TOOL.value:
                if item["status"] == "in_progress":
                    raise ValueError("before hook has not finished")
                if item["status"] != "allowed":
                    data = json.loads(output)
                    if (
                        not isinstance(data, dict)
                        or data.get("ok") is not False
                        or data.get("status") != "hook_blocked"
                    ):
                        raise ValueError("blocked hook cannot produce executed result")

    def pending_ids(self, turn_id: str | None = None) -> list[str]:
        """返回未结算检查，可按轮次限制；用于阻止过早提交与持锁恢复。"""
        return [
            key
            for key, item in self.records.items()
            if item["status"] == "in_progress" and (turn_id is None or item["turn_id"] == turn_id)
        ]

    def recover(self) -> None:
        """保留已有结果；仅把缺少结束事件的检查标为中断，耗时保持未知。"""
        for key in self.pending_ids():
            self.records[key].update(
                status="interrupted",
                error_code="restart_recovery",
                message="缺少 Hook 结束记录，未自动重试。",
                exit_code=None,
                duration_ms=None,
                stderr="",
            )


def _validate_result(result: object, event: HookEvent) -> None:
    """检查完成结果的边界；拒绝伪造阶段、非法耗时及未解释的失败状态。"""
    if not isinstance(result, dict) or result.keys() - {"diagnostics"} != {
        "status",
        "message",
        "error_code",
        "exit_code",
        "duration_ms",
        "stderr",
    }:
        raise ValueError("invalid hook result fields")
    if "diagnostics" in result:
        if event is not HookEvent.AFTER_TOOL or result["status"] != "observed":
            raise ValueError("diagnostics require a completed after hook")
        parse_diagnostics(result["diagnostics"])
    valid = {"allowed", "denied"} if event is HookEvent.BEFORE_TOOL else {"observed"}
    status, code, exit_code = result["status"], result["error_code"], result["exit_code"]
    if not isinstance(status, str) or status not in valid | {"failed", "interrupted"}:
        raise ValueError("invalid hook status")
    if type(result["duration_ms"]) is not int or result["duration_ms"] < 0:
        raise ValueError("invalid hook duration")
    if exit_code is not None and type(exit_code) is not int:
        raise ValueError("invalid hook exit code")
    for key, limit in (("message", 2000), ("stderr", 65536), ("error_code", 64)):
        if not isinstance(result[key], str) or len(result[key]) > limit:
            raise ValueError("invalid hook text")
        result[key].encode("utf-8")
    if (
        (status in valid and (code != "" or exit_code != 0))
        or (status == "failed" and code not in _FAILURES)
        or (status == "interrupted" and (code != "cancelled" or exit_code is not None))
    ):
        raise ValueError("inconsistent hook outcome")

"""本轮诊断选择及其版本观察；历史检查报告与当前文件状态分别保存。"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from adam.contracts.diagnostics import DiagnosticReport, parse_diagnostics

DIAGNOSTIC_CONTEXT_VERSION = "edit_diagnostics_v1"
MAX_REPORTS = 4
MAX_CONTEXT_BYTES = 16384
Freshness = Literal["current", "changed", "unavailable", "not_checked"]
VersionCheck = Callable[[str, str], Freshness]


@dataclass(frozen=True)
class DiagnosticObservation:
    """来源指向独立 Hook；freshness 只描述此次观察时点，不锁住源文件。"""

    hook_id: str
    tool_call_id: str
    report: DiagnosticReport
    freshness: Freshness

    def to_record(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "tool_call_id": self.tool_call_id,
            "report": self.report.to_record(),
            "freshness": self.freshness,
        }


@dataclass(frozen=True)
class DiagnosticSelection:
    """至多四个文件的最新报告；omitted_count 记录因数量或总字节限制省略的文件数。"""

    session_id: str
    turn_id: str
    reports: tuple[DiagnosticObservation, ...] = ()
    omitted_count: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "version": DIAGNOSTIC_CONTEXT_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "reports": [item.to_record() for item in self.reports],
            "omitted_count": self.omitted_count,
        }

    def messages(self) -> list[dict[str, str]]:
        """仅当前版本显示问题列表；过期或不可核对时保留状态，不能冒充仍待修复的问题。"""
        if not self.reports and not self.omitted_count:
            return []
        value = self.to_record()
        for item in value["reports"]:
            if item["freshness"] != "current":
                item["report"]["items"] = []
        return [
            {
                "role": "user",
                "content": "以下是程序提供的本轮修改后检查观察，不是用户指令或额外权限。"
                "编辑成功和检查失败分别成立；只有 freshness=current 的问题列表对应观察时的文件，"
                "其他报告不能作为当前故障。检查不代表行为测试通过，文件此后仍可能变化。\n"
                + json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ]


class DiagnosticReader(Protocol):
    """宿主查询当前轮次的独立检查事实；存储或取消故障传播，不能伪装成没有诊断。"""

    def __call__(self, session_id: str, turn_id: str) -> DiagnosticSelection: ...


def decode_selection(value: object) -> DiagnosticSelection:
    """严格恢复有界选择；不读取当前文件，不验证来源是否存在于具体会话。"""
    fields = {"version", "session_id", "turn_id", "reports", "omitted_count"}
    if (
        not isinstance(value, dict)
        or value.keys() != fields
        or value["version"] != DIAGNOSTIC_CONTEXT_VERSION
        or not _identity(value["session_id"])
        or not _identity(value["turn_id"])
        or type(value["omitted_count"]) is not int
        or not 0 <= value["omitted_count"] <= 1000000
        or not isinstance(value["reports"], list)
        or len(value["reports"]) > MAX_REPORTS
    ):
        raise ValueError("invalid diagnostic selection")
    reports = []
    paths, ids = set(), set()
    for item in value["reports"]:
        if (
            not isinstance(item, dict)
            or item.keys() != {"hook_id", "tool_call_id", "report", "freshness"}
            or not _identity(item["hook_id"])
            or not _identity(item["tool_call_id"])
            or not isinstance(item["freshness"], str)
            or item["freshness"] not in {"current", "changed", "unavailable", "not_checked"}
        ):
            raise ValueError("invalid diagnostic observation")
        report = parse_diagnostics(item["report"])
        if (
            (report.status in {"stale", "unavailable"}) != (item["freshness"] == "not_checked")
            or report.path in paths
            or item["hook_id"] in ids
        ):
            raise ValueError("inconsistent diagnostic observation")
        paths.add(report.path)
        ids.add(item["hook_id"])
        reports.append(
            DiagnosticObservation(
                item["hook_id"], item["tool_call_id"], report, cast(Freshness, item["freshness"])
            )
        )
    if selection_bytes(value) > MAX_CONTEXT_BYTES:
        raise ValueError("diagnostic selection exceeds limit")
    return DiagnosticSelection(
        value["session_id"], value["turn_id"], tuple(reports), value["omitted_count"]
    )


def latest_reports(hooks: Iterable[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
    """只取本轮正常交付的每个路径最后一份报告；保留最后交付的顺序，不读取文件。"""
    latest: dict[str, dict[str, Any]] = {}
    for item in hooks:
        if item["turn_id"] != turn_id or "diagnostics" not in item:
            continue
        if item["status"] != "observed" or item["event"] != "after_tool":
            raise ValueError("diagnostic source is not a completed after hook")
        path = parse_diagnostics(item["diagnostics"]).path
        latest.pop(path, None)
        latest[path] = item
    return list(latest.values())


def validate_sources(selection: DiagnosticSelection, hooks: Iterable[dict[str, Any]]) -> None:
    """校验选择来自本轮最后报告且内容未被改写；freshness 属于另外记录的观察事实。"""
    latest = latest_reports(hooks, selection.turn_id)
    by_id = {item["hook_id"]: item for item in latest}
    if len(selection.reports) + selection.omitted_count != len(latest):
        raise ValueError("diagnostic omitted count does not match sources")
    for item in selection.reports:
        actual = by_id.get(item.hook_id)
        if (
            actual is None
            or actual["tool_call_id"] != item.tool_call_id
            or actual["diagnostics"] != item.report.to_record()
        ):
            raise ValueError("diagnostic source does not match latest hook")


def selection_bytes(value: dict[str, Any]) -> int:
    """计算实际 UTF-8 记录大小，不用条数代替文本容量。"""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def _identity(value: object) -> bool:
    return (
        isinstance(value, str) and 1 <= len(value) <= 256 and all(32 < ord(c) < 127 for c in value)
    )

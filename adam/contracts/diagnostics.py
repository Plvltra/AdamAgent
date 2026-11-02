"""单个编辑版本的有界检查事实；不读取文件、不执行检查器，也不授予修改权限。"""

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

DiagnosticStatus = Literal["passed", "failed", "stale", "unavailable"]
Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Diagnostic:
    """一条检查意见；行列从1开始，column=None 表示检查器未提供列位置。"""

    line: int
    column: int | None
    severity: Severity
    code: str
    message: str


@dataclass(frozen=True)
class DiagnosticReport:
    """检查结果绑定输入编辑与实际读取的版本，不代表业务测试或权限批准。

    passed/failed 必须检查了 expected_version；stale 表示读到另一版本，unavailable
    表示未可靠读取。后两者不能携带冒充当前问题的诊断，也不把原编辑标记为失败。
    """

    checker: str
    path: str
    expected_version: str
    checked_version: str | None
    status: DiagnosticStatus
    items: tuple[Diagnostic, ...] = ()

    def to_record(self) -> dict[str, Any]:
        """生成独立 JSON 值；序列化边界仍须解析校验，不能信任直接构造的内容。"""
        value = asdict(self)
        value["items"] = list(value["items"])
        return value


def parse_diagnostics(value: object) -> DiagnosticReport:
    """验证最多16条/16KiB的完整报告；非法内容整体拒绝，不静默截断或修复状态。"""
    fields = {"checker", "path", "expected_version", "checked_version", "status", "items"}
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError("invalid diagnostic report fields")
    if not _identifier(value["checker"]) or not _text(value["path"], 4096):
        raise ValueError("invalid diagnostic source")
    expected, checked, status = value["expected_version"], value["checked_version"], value["status"]
    if not _version(expected) or (checked is not None and not _version(checked)):
        raise ValueError("invalid diagnostic version")
    if not isinstance(status, str) or status not in {"passed", "failed", "stale", "unavailable"}:
        raise ValueError("invalid diagnostic status")
    if not isinstance(value["items"], list) or len(value["items"]) > 16:
        raise ValueError("too many diagnostic items")
    items = tuple(_item(item) for item in value["items"])
    errors = any(item.severity == "error" for item in items)
    if (
        (status in {"passed", "failed"} and checked != expected)
        or (status == "passed" and errors)
        or (status == "failed" and not errors)
        or (status == "stale" and (checked is None or checked == expected or items))
        or (status == "unavailable" and (checked is not None or items))
    ):
        raise ValueError("inconsistent diagnostic outcome")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16384:
        raise ValueError("diagnostic report exceeds limit")
    return DiagnosticReport(
        value["checker"], value["path"], expected, checked, cast(DiagnosticStatus, status), items
    )


def match_edit(report: DiagnosticReport, tool_name: str, result: object) -> None:
    """报告必须对应本次已成功编辑的路径与目标版本；不从检查器声明推断文件当前状态。"""
    if (
        tool_name not in {"replace_text", "apply_proposal"}
        or not isinstance(result, dict)
        or result.get("status") != "applied"
        or result.get("path") != report.path
        or result.get("after_version") != report.expected_version
    ):
        raise ValueError("diagnostics do not match applied edit")


def _item(value: object) -> Diagnostic:
    if not isinstance(value, dict) or value.keys() != {
        "line",
        "column",
        "severity",
        "code",
        "message",
    }:
        raise ValueError("invalid diagnostic item fields")
    if type(value["line"]) is not int or not 1 <= value["line"] <= 1000000:
        raise ValueError("invalid diagnostic line")
    column = value["column"]
    if column is not None and (type(column) is not int or not 1 <= column <= 1000000):
        raise ValueError("invalid diagnostic column")
    if (
        not isinstance(value["severity"], str)
        or value["severity"] not in {"error", "warning", "info"}
        or not _identifier(value["code"])
        or not _text(value["message"], 500)
    ):
        raise ValueError("invalid diagnostic text or severity")
    return Diagnostic(
        value["line"], column, cast(Severity, value["severity"]), value["code"], value["message"]
    )


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None


def _version(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    value.encode("utf-8")
    return True

"""审查的结构化结论；证据存在不代表模型判断一定正确。"""

from typing import Any, Protocol


class Reviewer(Protocol):
    """模型只申请审查或收集结果，不能自行声明已通过。"""

    def request(self, goal: str, criteria: str, paths: list[str]) -> dict[str, Any]: ...
    def collect(self, review_id: str, timeout_seconds: int = 30) -> dict[str, Any]: ...


def validate_request(value: dict[str, Any]) -> None:
    """校验固定范围和来源；轮次/工具及审查次数由日志投影核对。"""
    fields = {
        "review_id",
        "turn_id",
        "request_id",
        "tool_call_id",
        "agent_id",
        "run_id",
        "round",
        "max_rounds",
        "goal",
        "criteria",
        "targets",
    }
    if set(value) != fields:
        raise ValueError("invalid review request fields")
    for key in fields - {"round", "max_rounds", "targets"}:
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 4000:
            raise ValueError("invalid review request text")
    if (
        type(value["round"]) is not int
        or type(value["max_rounds"]) is not int
        or not (1 <= value["round"] <= value["max_rounds"] <= 4)
    ):
        raise ValueError("invalid review round")
    targets = value["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise ValueError("invalid review targets")
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"path", "version"}:
            raise ValueError("invalid review target")
        if not isinstance(target["path"], str) or not target["path"] or target["path"] in seen:
            raise ValueError("invalid review path")
        version = target["version"]
        if (
            not isinstance(version, str)
            or len(version) != 64
            or any(c not in "0123456789abcdef" for c in version)
        ):
            raise ValueError("invalid file version")
        seen.add(target["path"])


def validate_result(
    value: object, reads: list[dict[str, Any]], targets: list[dict[str, str]]
) -> dict[str, Any]:
    """校验结论及独立读取依据；findings 的位置须实际读过，未全覆盖只能 incomplete。"""
    if not isinstance(value, dict) or set(value) != {"verdict", "summary", "findings", "evidence"}:
        raise ValueError("invalid review result")
    if value["verdict"] not in {"findings", "no_findings", "incomplete"}:
        raise ValueError("invalid review verdict")
    if not isinstance(value["summary"], str) or not 1 <= len(value["summary"]) <= 500:
        raise ValueError("invalid review summary")
    evidence = value["evidence"]
    available = {item["tool_call_id"]: item for item in reads}
    if not isinstance(evidence, list) or not all(
        isinstance(i, str) and i in available for i in evidence
    ):
        raise ValueError("unknown reviewer evidence")
    if len(evidence) != len(set(evidence)):
        raise ValueError("duplicate reviewer evidence")
    if value["verdict"] != "incomplete":
        checked = {(available[i]["path"], available[i]["version"]) for i in evidence}
        if value["verdict"] == "no_findings":
            checked = {
                (available[i]["path"], available[i]["version"])
                for i in evidence
                if available[i]["complete"]
            }
        if not evidence or any((item["path"], item["version"]) not in checked for item in targets):
            raise ValueError("review requires independent reads of every target")
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 8:
        raise ValueError("invalid findings")
    for item in findings:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "line",
            "severity",
            "message",
            "evidence_id",
        }:
            raise ValueError("invalid finding")
        if not isinstance(item["evidence_id"], str):
            raise ValueError("invalid evidence identity")
        source = available.get(item["evidence_id"], {})
        if (
            item["evidence_id"] not in evidence
            or item["path"] != source.get("path")
            or type(item["line"]) is not int
            or item["line"] not in source.get("lines", [])
            or item["severity"] not in {"high", "medium", "low"}
            or not isinstance(item["message"], str)
            or not 1 <= len(item["message"]) <= 500
        ):
            raise ValueError("finding requires actual file and line evidence")
    if (value["verdict"] == "findings") != bool(findings):
        raise ValueError("verdict contradicts findings")
    return dict(value)

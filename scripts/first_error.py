"""B32只读首错报告：可观察失败与根因推断分开，不把工具全成功当成任务正确。"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from adam.session.log import decode_records, parse_log


def analyze(path: Path, verdict_path: Path | None = None) -> dict[str, Any]:
    """校验日志后按物理行序找首次失败，附可核对证据和保守归因。

    verdict可使用B31单次结果，必须绑定同一session_id；不修改日志或请求模型。
    没有终态验证时，明确任务正确性未评估，不能把执行成功当作答案正确。
    """
    raw = path.read_bytes()
    log = parse_log(raw, path.stem)
    events = decode_records(raw)
    failures: list[dict[str, Any]] = []
    seen_tools: set[tuple[str, str, str]] = set()
    for line, event in enumerate(events, 1):
        payload, kind = event["payload"], event["type"]
        evidence = _failure(kind, payload)
        if evidence is None:
            continue
        if payload.get("tool_call_id"):
            key = (
                payload.get("turn_id", ""),
                payload.get("request_id", ""),
                payload["tool_call_id"],
            )
            if key in seen_tools:
                continue
            seen_tools.add(key)
        failures.append(
            {
                "line": line,
                "event": kind,
                "timestamp": event["timestamp"],
                "turn_id": payload.get("turn_id"),
                "request_id": payload.get("request_id"),
                "tool_call_id": payload.get("tool_call_id"),
                **evidence,
                "prefix_sha256": hashlib.sha256(
                    b"".join(raw.splitlines(keepends=True)[: line - 1])
                ).hexdigest(),
            }
        )
    verification = None
    verdict_identity = None
    if verdict_path is not None:
        verdict_raw = verdict_path.read_bytes()
        verdict = json.loads(verdict_raw)
        if verdict.get("session_id") != path.stem or not isinstance(
            verdict.get("verification"), dict
        ):
            raise ValueError("verdict must bind the same session and contain verification")
        verification = verdict["verification"]
        if type(verification.get("passed")) is not bool:
            raise ValueError("verdict requires a boolean passed field")
        verdict_identity = {
            "path": str(verdict_path.absolute()),
            "sha256": hashlib.sha256(verdict_raw).hexdigest(),
        }
    if verification is not None and verification["passed"]:
        primary = {
            "category": "no_verified_task_failure",
            "confidence": "high",
            "basis": "独立终态验证通过；日志中的工具失败可能已恢复，不归因为任务失败。",
        }
        secondary = failures
    elif failures:
        primary = {
            "category": failures[0]["category"],
            "confidence": "medium",
            "basis": "最早可观察失败是定位候选，不证明它是最终失败根因。",
            "evidence": failures[0],
        }
        secondary = failures[1:]
    elif verification is not None:
        primary = {
            "category": "final_verification_failed",
            "confidence": "high",
            "basis": "独立终态验证未通过，即使请求和工具没有可观察失败；更早错误决策位置未知。",
            "evidence": verification,
        }
        secondary = []
    else:
        primary = {
            "category": "insufficient_evidence",
            "confidence": "low",
            "basis": "没有可观察错误，也没有独立终态验证；不能声称任务成功。",
        }
        secondary = []
    return {
        "format": "adam_first_error_v1",
        "session_id": path.stem,
        "source": {
            "path": str(path.absolute()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "lines": len(events),
        },
        "verdict_source": verdict_identity,
        "verification": verification,
        "first_observed_error": failures[0] if failures else None,
        "primary": primary,
        "secondary": secondary,
        "root_cause": "not_proven",
        "unknown_usage": log.unknown_usage,
        "unfinished_turns": [
            key for key, state in log.turns.items() if state["status"] == "in_progress"
        ],
    }


def _failure(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """只识别明确终态与工具错误字段；缺失输出或普通文本不猜成失败。"""
    if kind in {"request_finished", "turn_finished"} and payload.get("status") in {
        "failed",
        "interrupted",
    }:
        return {
            "category": "request_failure" if kind == "request_finished" else "turn_failure",
            "reason": payload.get("reason"),
            "status": payload["status"],
            "error": payload.get("error"),
        }
    if kind not in {"tool_finished", "command_finished"}:
        return None
    output = payload
    if kind == "tool_finished":
        try:
            output = json.loads(payload["output"])
        except (ValueError, TypeError, KeyError):
            return None
    if not isinstance(output, dict):
        return None
    exit_code = output.get("exit_code")
    if type(exit_code) is int and exit_code != 0:
        return {
            "category": "nonzero_exit",
            "exit_code": exit_code,
            "status": output.get("status"),
            "note": "非零退出是观察事实；可能是预期探测，不能自动推断代码缺陷。",
        }
    if output.get("ok") is False or output.get("status") in {
        "denied",
        "failed",
        "timed_out",
        "cancelled",
        "start_failed",
    }:
        return {
            "category": "tool_failure",
            "status": output.get("status"),
            "error": str(output.get("error", ""))[:500],
        }
    return None


def main() -> None:
    """离线输出带物理行号和哈希的JSON报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--verdict", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log, args.verdict), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

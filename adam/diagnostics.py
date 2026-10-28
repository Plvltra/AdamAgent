"""独立的只读日志诊断入口；不加载模型配置、不获取写锁、不改变会话。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from adam.errors import AgentError
from adam.session_log import parse_log


def read_report(path: Path) -> dict[str, Any]:
    """读取一个 JSONL 快照，按轮次列出输入、请求、工具及结束信息。

    不创建目录、不修复文件，也不把诊断输入加入模型历史。正在写入的文件可能暂时
    不完整，此时明确报错；需要稳定快照时应在聊天进程退出后读取。
    无法读取或格式无效时抛出可展示的 AgentError，隐藏原始系统详情。
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AgentError("无法读取会话日志，请检查路径和权限。") from error
    try:
        log = parse_log(raw, path.stem)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise AgentError("会话日志格式无效或写入尚未完成；诊断未修改文件。") from None

    usages = {
        event["payload"]["request_id"]: {
            "input_tokens": event["payload"]["input_tokens"],
            "output_tokens": event["payload"]["output_tokens"],
        }
        for event in log.events
        if event["type"] == "request_usage"
    }
    turns = []
    for turn_id, turn in log.turns.items():
        turns.append(
            {
                "turn_id": turn_id,
                **_details(turn),
                "requests": [
                    {"request_id": key, **_details(value), "usage": usages.get(key)}
                    for key, value in log.requests.items()
                    if value["turn_id"] == turn_id
                ],
                "tools": [value for value in log.tools.values() if value["turn_id"] == turn_id],
                "commands": [
                    value for value in log.executions.values() if value["turn_id"] == turn_id
                ],
            }
        )
    return {
        "session_id": log.metadata["id"],
        "title": log.metadata["title"],
        "known_tokens": log.tokens,
        "unknown_usage": log.unknown_usage,
        "turns": turns,
        "recoveries": [event for event in log.events if event["type"] == "session_recovered"],
    }


def main(argv: list[str] | None = None) -> int:
    """以 JSON 输出只读诊断报告；成功返回 0，读取失败向 stderr 提示并返回 1。"""
    parser = argparse.ArgumentParser(description="只读查看 Adam 会话 JSONL 的执行轨迹")
    parser.add_argument("path", type=Path, help="会话 JSONL 文件路径")
    args = parser.parse_args(argv)
    try:
        report = read_report(args.path)
    except AgentError as error:
        print(error.user_message, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _details(state: dict[str, Any]) -> dict[str, Any]:
    """未结束或早期记录的详情显示 null，避免把缺失字段误认为成功或零耗时。"""
    return {"reason": None, "duration_ms": None, "error": None, **state}


if __name__ == "__main__":
    raise SystemExit(main())

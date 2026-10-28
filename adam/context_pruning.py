"""压力下缩短较旧工具结果的请求副本；原始事件、调用参数和工具配对不变。"""

import json
from copy import deepcopy
from typing import Any

from adam.config import ContextConfig
from adam.history import HistoryTurn

# 错误和退出状态不能因为正文缩减变成看似成功；过大的错误元数据宁可不缩减。
PRESERVED_FIELDS = ("ok", "status", "exit_code", "error", "stderr", "truncated", "next_line")


def prune_history(
    history: list[HistoryTurn],
    limits: ContextConfig,
) -> tuple[list[HistoryTurn], tuple[dict[str, Any], ...]]:
    """返回缩减副本和来源清单；仅较旧轮次可变，只有确实变短时采用替换。"""
    # 分别复制轮次，避免调用方复用消息列表时连带改动近期保护轮次。
    result = [HistoryTurn(turn.turn_id, deepcopy(turn.messages)) for turn in history]
    changes = []
    boundary = max(0, len(history) - limits.retain_recent_turns)
    for turn in result[:boundary]:
        for message in turn.messages:
            if message["role"] != "tool":
                continue
            original = message["content"]
            if len(original) <= limits.tool_prune_threshold_chars:
                continue
            source = {"turn_id": turn.turn_id, "tool_call_id": message["tool_call_id"]}
            replacement = _excerpt(original, source, limits)
            if len(replacement) >= len(original):
                continue
            message["content"] = replacement
            changes.append(
                {
                    **source,
                    "algorithm": "head_tail_v1",
                    "original_chars": len(original),
                    "result_chars": len(replacement),
                    "head_chars": limits.tool_prune_head_chars,
                    "tail_chars": limits.tool_prune_tail_chars,
                }
            )
    return result, tuple(changes)


def _excerpt(text: str, source: dict[str, str], limits: ContextConfig) -> str:
    """保留结构化状态，并用带来源的头尾节选替换正文；不能当作完整原始工具结果。"""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    metadata = (
        {key: data[key] for key in PRESERVED_FIELDS if key in data}
        if isinstance(data, dict)
        else {}
    )
    head = text[: limits.tool_prune_head_chars]
    tail = text[-limits.tool_prune_tail_chars :] if limits.tool_prune_tail_chars else ""
    return json.dumps(
        {
            **metadata,
            "context_pruned": True,
            "source": source,
            "excerpt": head + "\n…中间内容已省略；完整结果保留在会话日志…\n" + tail,
        },
        ensure_ascii=False,
    )

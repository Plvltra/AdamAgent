"""按最终 JSON 字符预算缩短工具文本，保留两端并明确展示层省略量。"""

import json
from typing import Any

from adam.contracts.errors import ToolError


def encode_head_tail(
    output: dict[str, Any],
    texts: dict[str, str],
    limit: int,
    *,
    optional: tuple[str, ...] = (),
) -> str:
    """返回有界 JSON 副本，不改变原结果或待保存附件。

    texts 给出需要缩短的原文本；每次从原文两端取，省略数按 Unicode 字符计，
    与上游已省略字节分开。只有全部文本放不下时才移除 optional 字段；仍不足则失败。
    """
    result = {**output, **texts}
    kept = {key: len(value) for key, value in texts.items()}
    while True:
        # 空白不增加证据信息；小额度优先留给真实尾部与状态，JSON 结构保持完整。
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            return encoded
        key = max(kept, key=lambda item: kept[item], default=None)
        if key is not None and kept[key]:
            kept[key] //= 2
            result[key] = _ends(texts[key], kept[key])
            result["preview_omitted_chars"] = {
                name: len(texts[name]) - count
                for name, count in kept.items()
                if count < len(texts[name])
            }
            result["truncated"] = True
            continue
        removable = next((name for name in optional if name in result), None)
        if removable is None:
            raise ToolError("结果上限不足以容纳状态、来源及省略信息。")
        del result[removable]


def _ends(text: str, count: int) -> str:
    """计数只包含原文两端；零容量时省略量仍由外层字段保留。"""
    if not count:
        return ""
    head, tail = (count + 1) // 2, count // 2
    marker = f"\n[...预览省略 {len(text) - count} 字符...]\n"
    return text[:head] + marker + (text[-tail:] if tail else "")

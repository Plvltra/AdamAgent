"""本地请求输入的字节观测；不改变请求，不预测服务端 tokenizer 或 KV 缓存布局。"""

import json
import secrets
from hashlib import sha256
from typing import Any

from adam.contracts.usage import INPUT_LAYOUT_VERSION, InputPrefix


class PrefixTracker:
    """串行比较当前客户端的上一份输入；主子各自独立，重建后不读取历史原文。"""

    def __init__(self) -> None:
        self.scope_id = secrets.token_hex(8)
        self.sequence = 0
        self.previous: tuple[InputPrefix, bytes] | None = None

    def observe(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> InputPrefix:
        """记录一次输入准备；换模型不比较，返回长度/哈希，原文仅留最后一份在内存。

        v1 顺序固定为模型、工具、消息，每块 JSON 键排序；工具/消息数组的顺序保持。
        这不是 HTTP 字节，更不是服务端展开后的提示词；连接失败也属于输入准备观察。
        """
        raw = json.dumps(
            [{"model": model}, {"tools": tools or []}, {"messages": messages}],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        sequence = self.sequence + 1
        previous = self.previous
        comparison: dict[str, Any] = {}
        if (
            previous
            and previous[0].model == model
            and previous[0].layout_version == INPUT_LAYOUT_VERSION
        ):
            before, data = previous
            count = 0
            for left, right in zip(data, raw, strict=False):
                if left != right:
                    break
                count += 1
            comparison = {
                "previous_sequence": before.sequence,
                "previous_bytes": before.input_bytes,
                "previous_sha256": before.input_sha256,
                "common_prefix_bytes": count,
            }
        result = InputPrefix(
            self.scope_id, sequence, model, len(raw), sha256(raw).hexdigest(), **comparison
        )
        self.sequence = sequence
        self.previous = (result, raw)
        return result

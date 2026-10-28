"""解析 Chat Completions SSE；增量只用于展示，完整消息才允许进入工具循环。"""

import json
from collections.abc import Iterable, Iterator
from typing import Any

from adam.models import Completion
from adam.protocol import validate_assistant

MAX_EVENT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def sse_events(chunks: Iterable[bytes]) -> Iterator[str]:
    """按空行切分 SSE data 事件，接受网络和 UTF-8 字符任意分片。

    注释/其他 SSE 字段不属于模型数据；支持多行 data 和 CRLF。
    单行、单事件及总响应均有硬上限；残缺尾事件抛 ValueError，不补造结束标记。
    """
    pending = bytearray()
    data: list[bytes] = []
    event_size = total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("line too large")
            line = line.removesuffix(b"\r")
            if not line:
                if data:
                    yield b"\n".join(data).decode("utf-8")
                data, event_size = [], 0
            elif line.startswith(b"data:"):
                value = line[5:].removeprefix(b" ")
                event_size += len(value)
                if event_size > MAX_EVENT_BYTES:
                    raise ValueError("event too large")
                data.append(bytes(value))
        if len(pending) > MAX_EVENT_BYTES:
            raise ValueError("line too large")
    if pending or data:
        raise ValueError("incomplete event")


def parse_usage(value: object) -> tuple[int, int] | None:
    """仅接受服务端报告的非负整数用量；缺失或非法值保持未知。"""
    if isinstance(value, dict):
        inputs, outputs = value.get("prompt_tokens"), value.get("completion_tokens")
        if type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
            return inputs, outputs
    return None


class StreamReply:
    """组装一个候选回答；保留推理字段供协议回传，但只返回可展示的正文增量。"""

    def __init__(self) -> None:
        self.usage: tuple[int, int] | None = None
        self.message: dict[str, Any] = {"role": "assistant", "content": None}
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None

    def accept(self, event: str) -> str:
        """消费一个 JSON 事件，返回本片正文；结构或结束顺序错误抛 ValueError。"""
        data = json.loads(event)
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("invalid event")
        if data.get("usage") is not None:
            self.usage = parse_usage(data["usage"])
        choices = data["choices"]
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("invalid choices")
        if not choices:  # 兼容使用独立 usage 事件的 Chat Completions 服务。
            return ""
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0:
            raise ValueError("unsupported choice")
        delta = choice["delta"]
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise ValueError("invalid delta")
        if self.finish_reason is not None:
            raise ValueError("delta after finish")
        text = ""
        for key in ("content", "reasoning_content"):
            value = delta.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("invalid text")
                self.message[key] = (self.message.get(key) or "") + value
                if key == "content":
                    text = value
        calls = delta.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("invalid calls")
        for call in calls:
            self._append_call(call)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise ValueError("invalid finish")
            self.finish_reason = reason
        return text

    def finish(self) -> Completion:
        """仅在收到 [DONE] 后调用；完整性和工具配对格式通过后返回响应。"""
        if self.calls:
            if sorted(self.calls) != list(range(len(self.calls))):
                raise ValueError("missing tool index")
            self.message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        validate_assistant(self.message)
        expected = "tool_calls" if self.calls else "stop"
        if self.finish_reason != expected:
            raise ValueError("unfinished response")
        return Completion(self.message, self.usage)

    def _append_call(self, fragment: dict[str, Any]) -> None:
        """按 index 拼接工具 ID、名称和 JSON 参数；此处不执行任何工具。"""
        index = fragment["index"]
        if type(index) is not int or not 0 <= index < 128:
            raise ValueError("invalid tool index")
        call = self.calls.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if fragment.get("type") not in (None, "function"):
            raise ValueError("invalid tool type")
        for key, source, target in (
            ("id", fragment, call),
            ("name", fragment.get("function") or {}, call["function"]),
            ("arguments", fragment.get("function") or {}, call["function"]),
        ):
            value = source.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("invalid tool fragment")
                target[key] += value

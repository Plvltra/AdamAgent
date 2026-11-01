"""版本化单轮录制协议；仅验证 JSON 数据和调用顺序，不加载代码或执行工具。"""

import json
import math
from dataclasses import dataclass
from typing import Any

from adam.contracts.errors import (
    AgentError,
    FailureCode,
    LLMError,
    RequestInterrupted,
    RequestNotSentError,
)
from adam.contracts.media import MediaRef
from adam.contracts.model_input import message_media
from adam.contracts.models import ModelOutput
from adam.contracts.protocol import validate_assistant
from adam.contracts.tool_catalog import tool_schema_record
from adam.contracts.usage import OBSERVATION_FIELDS, decode_observations, encode_observations
from adam.recording.snapshot import validate_seed
from adam.session.log import SessionLog


class RecordingError(AgentError):
    """录制文件无效或写入失败；不能据此重试已经执行的模型或工具。"""


@dataclass(frozen=True)
class RecordingLimits:
    """文件、单条记录和事件数的上限；拒绝超限，不静默截断可重放的数据。"""

    max_bytes: int = 32 * 1024 * 1024
    max_event_bytes: int = 4 * 1024 * 1024
    max_events: int = 10000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_bytes, self.max_event_bytes, self.max_events)
        ):
            raise ValueError("录制上限必须是正整数。")
        if self.max_event_bytes > self.max_bytes or self.max_bytes > 256 * 1024 * 1024:
            raise ValueError("录制单条上限不能超过文件上限，文件最多256 MiB。")
        if self.max_events > 100000:
            raise ValueError("录制事件最多100000条。")


def error_record(error: BaseException) -> dict[str, Any]:
    """记录受控错误类别、已知用量和输入观测；不序列化原始异常文字或任意对象属性。"""
    if isinstance(error, RequestNotSentError):
        kind, code = "not_sent", FailureCode.API_NOT_SENT
    elif isinstance(error, RequestInterrupted):
        kind, code = "request_interrupted", FailureCode.INTERRUPTED
    elif isinstance(error, KeyboardInterrupt):
        kind, code = "interrupted", FailureCode.INTERRUPTED
    elif isinstance(error, SystemExit):
        kind, code = "exit", FailureCode.PROCESS_EXIT
    elif isinstance(error, LLMError):
        kind, code = "llm", error.code
    elif isinstance(error, AgentError):
        kind, code = "agent", error.code
    else:
        kind, code = "internal", FailureCode.INTERNAL_ERROR
    usage = error.usage if isinstance(error, (LLMError, RequestInterrupted)) else None
    return {
        "kind": kind,
        "code": code.value,
        "usage": list(usage) if usage is not None else None,
        "http_status": error.http_status if isinstance(error, AgentError) else None,
        **(
            encode_observations(error.cache_usage, error.input_prefix)
            if isinstance(error, (LLMError, RequestInterrupted))
            else {}
        ),
    }


def _fields(
    value: object, keys: set[str], *, optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """校验必需字段和明确声明的可选字段；其他字段仍严格拒绝。"""
    if not isinstance(value, dict) or not keys <= value.keys() or value.keys() - keys - optional:
        raise ValueError("invalid recording fields")
    return value


def _usage(value: object) -> None:
    if value is not None and (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or not 0 <= item <= 1_000_000_000 for item in value)
    ):
        raise ValueError("invalid recording usage")


def _error(value: object) -> None:
    """只允许受控类别和数值状态；未发出请求必须保留零用量语义。"""
    item = _fields(value, {"kind", "code", "usage", "http_status"}, optional=OBSERVATION_FIELDS)
    if item["kind"] not in {
        "not_sent",
        "request_interrupted",
        "interrupted",
        "exit",
        "llm",
        "agent",
        "internal",
    }:
        raise ValueError("invalid recording error kind")
    FailureCode(item["code"])
    _usage(item["usage"])
    if OBSERVATION_FIELDS & item.keys() and item["kind"] not in {
        "llm",
        "not_sent",
        "request_interrupted",
    }:
        raise ValueError("non-model error contains model observation")
    decode_observations(item, item["usage"][0] if item["usage"] is not None else None)
    status = item["http_status"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("invalid recording HTTP status")
    if item["kind"] == "not_sent" and (item["code"] != "api_not_sent" or item["usage"] != [0, 0]):
        raise ValueError("invalid unsent request")


def _tree(value: object, depth: int = 0, remaining: list[int] | None = None) -> None:
    """限制嵌套与节点数，拒绝非 JSON 类型、非有限数和不能编码为 UTF-8 的文本。"""
    if remaining is None:
        remaining = [200000]
    remaining[0] -= 1
    if depth > 32 or remaining[0] < 0:
        raise ValueError("recording JSON complexity limit")
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        value.encode("utf-8")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite recording number")
    elif isinstance(value, list):
        for item in value:
            _tree(item, depth + 1, remaining)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("non-text recording key")
            _tree(key, depth + 1, remaining)
            _tree(item, depth + 1, remaining)
    else:
        raise ValueError("non-JSON recording value")


def encode_event(event: dict[str, Any]) -> bytes:
    """编码一条有界协议记录；业务字段另外由 TraceState 校验。"""
    _tree(event)
    return (
        json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def decode_event(raw: bytes) -> dict[str, Any]:
    """严格解码一行；重复字段、非有限数与过深 JSON 均无效。"""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate recording key")
            value[key] = item
        return value

    def constant(_: str) -> None:
        raise ValueError("non-finite JSON constant")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    _tree(value)
    return _fields(value, {"seq", "type", "data"})


class TraceState:
    """逐条检查一个轮次的录制；只有明确终态且无未结束调用时才完整。"""

    def __init__(self) -> None:
        self.count = 0
        self.pending: tuple[int, str, dict[str, Any]] | None = None
        self.closed = False
        self.log: SessionLog | None = None
        self.seed_turns: set[str] = set()
        self.session_id = ""
        self.user_text = ""
        self.last_calls: list[dict[str, Any]] = []
        self.last_tool_names: list[str] = []
        self.output_ended = False
        self.media: dict[str, MediaRef] = {}
        self.input_media_seen = False

    def accept(self, event: dict[str, Any]) -> None:
        """消费下一条记录；失败后调用方丢弃此状态，不继续写入。"""
        item = _fields(event, {"seq", "type", "data"})
        if type(item["seq"]) is not int or item["seq"] != self.count or self.closed:
            raise ValueError("invalid recording sequence")
        kind, data = item["type"], item["data"]
        if self.count == 0:
            _fields(data, {"version", "seed"})
            if (
                kind != "recording_started"
                or type(data["version"]) is not int
                or data["version"] != 1
            ):
                raise ValueError("unsupported recording version")
            validate_seed(data["seed"])
            seed = data["seed"]
            self.log = SessionLog(seed["session_events"][0]["payload"])
            self.log.extend(seed["session_events"][1:])
            self.seed_turns = set(self.log.turns)
            self.session_id, self.user_text = seed["session_id"], seed["user_text"]
        elif kind == "call_started":
            self._start(data)
        elif kind == "model_output":
            _fields(data, {"call_id", "kind", "text"})
            self._pending(data["call_id"], "model")
            if self.output_ended:
                raise ValueError("output after response end")
            if data["kind"] not in {"text", "response_end"} or not isinstance(data["text"], str):
                raise ValueError("invalid model output")
            if data["kind"] == "response_end" and data["text"]:
                raise ValueError("response end contains text")
            self.output_ended = data["kind"] == "response_end"
        elif kind == "call_finished":
            self._finish(data)
        elif kind == "observation":
            _fields(data, {"channel", "value"})
            if data["channel"] not in {
                "context",
                "memory",
                "rules",
                "delegation",
                "review",
                "accounting",
                "skill",
            }:
                raise ValueError("invalid observation channel")
            if not isinstance(data["value"], dict):
                raise ValueError("invalid observation value")
            value = data["value"]
            if data["channel"] == "context" and value.get("operation") == "input_media":
                validate_exchange(value)
                if (
                    self.input_media_seen
                    or self.pending is not None
                    or value["error"] is not None
                    or value["value"] is not None
                    or value["input"].keys() != {"media"}
                    or self.log is None
                    or set(self.log.turns) != self.seed_turns
                ):
                    raise ValueError("invalid input media observation")
                self._media({"role": "user", **value["input"]})
                self.input_media_seen = True
        elif kind == "recording_finished":
            _fields(data, {"reply", "error", "session_events"})
            if self.pending is not None:
                raise ValueError("unfinished recording call")
            if data["error"] is None:
                if not isinstance(data["reply"], str):
                    raise ValueError("missing recorded reply")
            else:
                _error(data["error"])
                if data["reply"] is not None:
                    raise ValueError("failed recording with successful reply")
            self._events(data["session_events"])
            assert self.log is not None
            turns = set(self.log.turns) - self.seed_turns
            if data["error"] is None and (
                len(turns) != 1
                or self.log.turns[next(iter(turns))]["status"] != "succeeded"
                or not self.log.messages
                or self.log.messages[-1][1].get("content") != data["reply"]
            ):
                raise ValueError("successful recording has no committed matching answer")
            self.closed = True
        else:
            raise ValueError("unknown recording event")
        self.count += 1

    def _start(self, data: object) -> None:
        """调用按主线程顺序进入；工具批次必须来自最近完整模型响应。"""
        item = _fields(data, {"call_id", "name", "input"})
        if (
            self.pending is not None
            or type(item["call_id"]) is not int
            or item["call_id"] != self.count
        ):
            raise ValueError("overlapping recording calls")
        value = item["input"]
        if item["name"] == "model":
            _fields(value, {"messages", "model", "tools", "max_tokens", "on_output"})
            if not isinstance(value["messages"], list) or not all(
                isinstance(msg, dict) for msg in value["messages"]
            ):
                raise ValueError("invalid model messages")
            for message in value["messages"]:
                self._media(message)
            if not isinstance(value["model"], str) or not value["model"].strip():
                raise ValueError("invalid model ID")
            if value["tools"] is not None and not isinstance(value["tools"], list):
                raise ValueError("invalid tools description")
            if (
                type(value["max_tokens"]) is not int
                or value["max_tokens"] <= 0
                or type(value["on_output"]) is not bool
            ):
                raise ValueError("invalid model options")
        elif item["name"] == "tools":
            _fields(
                value,
                {
                    "session_id",
                    "turn_id",
                    "request_id",
                    "calls",
                    "edit_attempts",
                    "max_edit_attempts",
                    *(
                        {"provided_names"}
                        if isinstance(value, dict) and "provided_names" in value
                        else set()
                    ),
                },
            )
            for key, size in (("session_id", 8), ("turn_id", 32), ("request_id", 32)):
                text = value[key]
                if (
                    not isinstance(text, str)
                    or len(text) != size
                    or any(c not in "0123456789abcdef" for c in text)
                ):
                    raise ValueError("invalid tool source")
            if value["session_id"] != self.session_id or value["calls"] != self.last_calls:
                raise ValueError("tool batch does not match recorded model request")
            if not value["calls"]:
                raise ValueError("empty tools batch")
            if "provided_names" in value and value["provided_names"] != self.last_tool_names:
                raise ValueError("tool visibility differs from its actual model request")
            validate_assistant({"role": "assistant", "content": None, "tool_calls": value["calls"]})
            if any(
                type(value[key]) is not int or value[key] < 0
                for key in ("edit_attempts", "max_edit_attempts")
            ):
                raise ValueError("invalid edit budget")
        else:
            raise ValueError("unknown recording call")
        self.pending = (item["call_id"], item["name"], value)
        self.output_ended = False

    def _finish(self, data: object) -> None:
        """核对调用身份、协议结果和实际工具日志，再允许下一次调用。"""
        item = _fields(data, {"call_id", "value", "error", "session_events"})
        self._pending(item["call_id"])
        assert self.pending is not None
        _, name, arguments = self.pending
        if item["error"] is not None:
            _error(item["error"])
            if item["value"] is not None:
                raise ValueError("failed call contains result")
        elif name == "model":
            result = _fields(item["value"], {"message", "usage"}, optional=OBSERVATION_FIELDS)
            if not isinstance(result["message"], dict):
                raise ValueError("invalid model completion")
            validate_assistant(result["message"])
            _usage(result["usage"])
            decode_observations(
                result,
                result["usage"][0] if result["usage"] is not None else None,
                model=arguments["model"],
            )
            self.last_calls = result["message"].get("tool_calls") or []
            self.last_tool_names = tool_schema_record(arguments["tools"] or [])["names"]
        else:
            messages = item["value"]
            calls = arguments["calls"]
            if not isinstance(messages, list) or len(messages) != len(calls):
                raise ValueError("unmatched tool results")
            for message, call in zip(messages, calls, strict=True):
                _fields(message, {"role", "tool_call_id", "content"})
                if (
                    message["role"] != "tool"
                    or message["tool_call_id"] != call["id"]
                    or not isinstance(message["content"], str)
                ):
                    raise ValueError("unmatched tool result")
        self._events(item["session_events"])
        if name == "tools" and item["error"] is None:
            assert self.log is not None
            assert isinstance(item["value"], list)  # 前面的工具结果校验已保证此类型。
            for message, call in zip(item["value"], arguments["calls"], strict=True):
                actual = self.log.tools.get((arguments["turn_id"], call["id"]))
                if (
                    actual is None
                    or actual["request_id"] != arguments["request_id"]
                    or actual["name"] != call["function"]["name"]
                    or actual["arguments"] != call["function"]["arguments"]
                    or not actual["finished"]
                    or actual["output"] != message["content"]
                ):
                    raise ValueError("recorded tool result differs from execution log")
        self.pending = None

    def _pending(self, call_id: object, name: str | None = None) -> None:
        if (
            type(call_id) is not int
            or self.pending is None
            or self.pending[0] != call_id
            or (name is not None and self.pending[1] != name)
        ):
            raise ValueError("recording result without matching call")

    def _media(self, message: dict[str, Any]) -> None:
        """只认当前已采集会话的来源，不把工具结果或其他会话复制来的引用当图像。"""
        assert self.log is not None
        for reference in message_media(message):
            if (
                reference.source.session_id != self.session_id
                or self.log.media.references.get(reference.media_id) != reference
            ):
                raise ValueError("recorded media lacks committed source")
            self.media[reference.media_id] = reference

    def _events(self, events: object) -> None:
        """用生产 SessionLog 校验增量，拒绝混入另一输入或第二个用户轮次。"""
        if not isinstance(events, list):
            raise ValueError("invalid session event delta")
        for event in events:
            item = _fields(event, {"type", "timestamp", "payload"})
            if (
                not isinstance(item["type"], str)
                or not isinstance(item["timestamp"], str)
                or not isinstance(item["payload"], dict)
            ):
                raise ValueError("invalid session event")
        assert self.log is not None
        self.log.extend(events)
        added = set(self.log.turns) - self.seed_turns
        if len(added) > 1 or any(
            self.log.turns[turn]["user_text"] != self.user_text for turn in added
        ):
            raise ValueError("recording spans more than the selected turn")

    @staticmethod
    def output(call_id: int, output: ModelOutput) -> dict[str, Any]:
        return {"call_id": call_id, "kind": output.kind, "text": output.text}


def validate_exchange(value: object) -> None:
    """校验资料调用的输入、结果及受控错误；内容只作数据，不按名称动态导入或调用。"""
    item = _fields(value, {"operation", "input", "value", "error"})
    if not isinstance(item["operation"], str) or not item["operation"]:
        raise ValueError("invalid observation operation")
    if not isinstance(item["input"], dict):
        raise ValueError("invalid observation input")
    if item["error"] is not None:
        _error(item["error"])
        if item["value"] is not None:
            raise ValueError("failed observation has a result")

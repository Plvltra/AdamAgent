"""JSONL v4 事件校验与状态投影；诊断记录和有效模型历史分别归并。"""

import json
from typing import Any

from adam.outcome import outcome_details
from adam.protocol import validate_turn


class UnsupportedLogVersionError(ValueError):
    """日志版本不受支持；调用方不得猜测字段含义或自动迁移。"""


def decode_records(raw: bytes) -> list[dict[str, Any]]:
    """解码完整 JSONL 字节块；残缺行、非法外层结构抛出 ValueError。"""
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("missing newline")
    entries = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("type"), str)
            or not isinstance(entry.get("timestamp"), str)
            or not isinstance(entry.get("payload"), dict)
        ):
            raise ValueError("invalid record")
    return entries


def parse_log(raw: bytes, session_id: str) -> "SessionLog":
    """校验 v4 元信息及全部事件；不完整诊断记录可读，半个成功消息块不可读。"""
    entries = decode_records(raw)
    meta = entries[0]
    data = meta["payload"]
    if (
        meta["type"] != "session_meta"
        or type(data.get("version")) is not int
        or data["version"] != 4
    ):
        raise UnsupportedLogVersionError("unsupported session version")
    if data.get("id") != session_id or not all(
        isinstance(data.get(key), str) for key in ("title", "system_prompt")
    ):
        raise ValueError("invalid metadata")
    log = SessionLog(data)
    log.events.append(meta)
    log.extend(entries[1:])
    return log


class SessionLog:
    """一次读取的事件投影；不访问文件，不推断失败原因或自动结束遗留操作。

    messages 只包含提交完成的历史；turns、requests、tools、executions 保留诊断关联。
    每次读取重新构建，避免内存状态先于持久化成功而被误用。
    """

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        self.events: list[dict[str, Any]] = []
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.turns: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.tokens = 0
        self._unknown_usage = False
        self._pending_turn = ""
        self._pending_messages: list[dict[str, Any]] = []

    @property
    def unknown_usage(self) -> bool:
        """未知用量或未结算请求均阻止继续消费，不将缺失记录解释为零。"""
        return self._unknown_usage or any(
            not request["settled"] for request in self.requests.values()
        )

    def extend(self, entries: list[dict[str, Any]]) -> None:
        """逐条验证关联并更新投影；完整批次不能留下半个成功消息块。

        未完成 turn_started/tool_started 属于诊断，不加入待提交消息，因此允许重开读取。
        调用方仅在本方法成功后将整个批次写入文件；失败时丢弃此投影。
        """
        handlers = {
            "turn_started": self._start_turn,
            "request_started": self._apply_request,
            "request_usage": self._apply_request,
            "request_finished": self._finish_request,
            "turn_finished": self._finish_turn,
            "tool_started": self._apply_tool,
            "tool_finished": self._apply_tool,
            "command_started": self._apply_command,
            "command_finished": self._apply_command,
            "command_denied": self._apply_command,
            "message": self._apply_message,
            "turn_completed": self._complete_turn,
        }
        for entry in entries:
            kind, payload = entry["type"], entry["payload"]
            if kind not in handlers:
                raise ValueError("unknown event")
            if self._pending_messages and kind not in {"message", "turn_completed"}:
                raise ValueError("interleaved message block")
            handlers[kind](kind, payload)
            self.events.append(entry)
        if self._pending_messages:
            raise ValueError("incomplete turn")

    def _start_turn(self, kind: str, payload: dict[str, Any]) -> None:
        turn_id = self._identifier(payload, "turn_id")
        if turn_id in self.turns or not isinstance(payload.get("user_text"), str):
            raise ValueError("invalid or duplicate turn")
        self.turns[turn_id] = {"user_text": payload["user_text"], "status": "in_progress"}

    def _apply_request(self, kind: str, payload: dict[str, Any]) -> None:
        """同轮请求只可开始、结算一次；用量未知与轮次是否完成分别维护。"""
        turn_id = self._require_turn(payload)
        request_id = self._identifier(payload, "request_id")
        if kind == "request_started":
            if request_id in self.requests or not isinstance(payload.get("model"), str):
                raise ValueError("invalid or duplicate request")
            self.requests[request_id] = {
                "turn_id": turn_id,
                "model": payload["model"],
                "settled": False,
                "status": "in_progress",
            }
            return
        request = self._require_request(payload)
        if request["settled"] or request["status"] != "in_progress":
            raise ValueError("duplicate usage")
        inputs, outputs = payload.get("input_tokens"), payload.get("output_tokens")
        if "input_tokens" not in payload or "output_tokens" not in payload:
            raise ValueError("missing usage")
        if inputs is None and outputs is None:
            self._unknown_usage = True
        elif type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
            self.tokens += inputs + outputs
        else:
            raise ValueError("invalid usage")
        request["settled"] = True

    def _finish_request(self, kind: str, payload: dict[str, Any]) -> None:
        """请求状态与用量分别校验；只有有效模型响应才由调用方标记 succeeded。"""
        self._require_turn(payload)
        request = self._require_request(payload)
        status = payload.get("status")
        if (
            request["status"] != "in_progress"
            or not request["settled"]
            or status not in {"succeeded", "failed", "interrupted"}
        ):
            raise ValueError("invalid request transition")
        request.update(outcome_details(payload, status))
        request["status"] = status

    def _finish_turn(self, kind: str, payload: dict[str, Any]) -> None:
        """失败或中断关闭诊断轮次；成功只能由完整消息块的提交标记产生。"""
        turn_id = self._require_turn(payload)
        status = payload.get("status")
        if status not in {"failed", "interrupted"}:
            raise ValueError("invalid turn transition")
        self.turns[turn_id].update(outcome_details(payload, status))
        self.turns[turn_id]["status"] = status

    def _apply_tool(self, kind: str, payload: dict[str, Any]) -> None:
        """工具 ID 在轮次内唯一；结束记录必须与开始时的请求和轮次一致。"""
        turn_id = self._require_turn(payload)
        request = self._require_request(payload)
        if request["status"] != "succeeded":
            raise ValueError("tool requires a successful request")
        call_id = self._identifier(payload, "tool_call_id")
        key = (turn_id, call_id)
        if kind == "tool_started":
            if (
                key in self.tools
                or not isinstance(payload.get("name"), str)
                or not payload["name"]
                or not isinstance(payload.get("arguments"), str)
            ):
                raise ValueError("invalid or duplicate tool")
            self.tools[key] = {
                **payload,
                "finished": False,
                "execution_id": None,
            }
            return
        tool = self._require_tool(payload)
        if tool["finished"] or not isinstance(payload.get("output"), str):
            raise ValueError("invalid or duplicate tool finish")
        execution_id = tool["execution_id"]
        if execution_id is not None and self.executions[execution_id]["status"] == "unknown":
            raise ValueError("command has no final result")
        tool.update(output=payload["output"], finished=True)

    def _apply_command(self, kind: str, payload: dict[str, Any]) -> None:
        """命令事件属于已开始的 run_command 工具；缺少结束事件时保持 unknown。"""
        self._require_turn(payload)
        self._require_request(payload)
        tool = self._require_tool(payload)
        if tool["name"] != "run_command" or tool["finished"]:
            raise ValueError("invalid command parent")
        execution_id = self._identifier(payload, "execution_id")
        if kind in {"command_started", "command_denied"}:
            if execution_id in self.executions or tool["execution_id"] is not None:
                raise ValueError("duplicate execution")
            if (
                not isinstance(payload.get("argv"), list)
                or not payload["argv"]
                or not all(isinstance(arg, str) for arg in payload["argv"])
                or not isinstance(payload.get("cwd"), str)
            ):
                raise ValueError("invalid command")
            if kind == "command_denied" and payload.get("status") != "denied":
                raise ValueError("invalid denial")
            self.executions[execution_id] = {
                **payload,
                "status": "unknown" if kind == "command_started" else "denied",
            }
            tool["execution_id"] = execution_id
            return
        previous = self.executions.get(execution_id)
        if previous is None or previous["status"] != "unknown":
            raise ValueError("unmatched command finish")
        if tool["execution_id"] != execution_id or any(
            payload.get(key) != previous[key] for key in ("turn_id", "request_id", "tool_call_id")
        ):
            raise ValueError("mismatched command parent")
        if payload.get("status") not in {"completed", "timed_out", "start_failed", "interrupted"}:
            raise ValueError("invalid command status")
        if payload.get("exit_code") is not None and type(payload["exit_code"]) is not int:
            raise ValueError("invalid exit code")
        self.executions[execution_id] = {**previous, **payload}

    def _apply_message(self, kind: str, payload: dict[str, Any]) -> None:
        turn_id = self._require_turn(payload)
        if self._pending_messages and turn_id != self._pending_turn:
            raise ValueError("incomplete turn")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid message")
        self._pending_turn = turn_id
        self._pending_messages.append(message)

    def _complete_turn(self, kind: str, payload: dict[str, Any]) -> None:
        """完成标记同时验证协议与诊断关联，再将消息纳入有效上下文。"""
        turn_id = self._require_turn(payload)
        if payload.get("status", "succeeded") != "succeeded":
            raise ValueError("completion requires success")
        details = outcome_details(payload, "succeeded")
        if not self._pending_messages or self._pending_turn != turn_id:
            raise ValueError("unmatched completion")
        validate_turn(self._pending_messages)
        if self._pending_messages[0]["content"] != self.turns[turn_id]["user_text"]:
            raise ValueError("mismatched user input")
        if any(
            request["turn_id"] == turn_id
            and (not request["settled"] or request["status"] != "succeeded")
            for request in self.requests.values()
        ):
            raise ValueError("request did not succeed")
        turn_tools = {key[1]: tool for key, tool in self.tools.items() if key[0] == turn_id}
        seen: set[str] = set()
        for message in self._pending_messages[1:]:
            for call in message.get("tool_calls") or []:
                tool = turn_tools.get(call["id"])
                if tool is None or any(
                    call["function"][field] != tool[field] for field in ("name", "arguments")
                ):
                    raise ValueError("mismatched tool request")
                seen.add(call["id"])
            if message["role"] == "tool":
                tool = turn_tools.get(message["tool_call_id"])
                if tool is None or not tool["finished"] or message["content"] != tool["output"]:
                    raise ValueError("mismatched tool output")
        if seen != set(turn_tools):
            raise ValueError("uncommitted tool trace")
        self.messages.extend((turn_id, message) for message in self._pending_messages)
        self.turns[turn_id].update(details)
        self.turns[turn_id]["status"] = "succeeded"
        self._pending_messages = []
        self._pending_turn = ""

    def _require_turn(self, payload: dict[str, Any]) -> str:
        turn_id = self._identifier(payload, "turn_id")
        if turn_id not in self.turns or self.turns[turn_id]["status"] != "in_progress":
            raise ValueError("unknown or finished turn")
        return turn_id

    def _require_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self.requests.get(self._identifier(payload, "request_id"))
        if request is None or request["turn_id"] != payload["turn_id"]:
            raise ValueError("unknown or mismatched request")
        return request

    def _require_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get((payload["turn_id"], self._identifier(payload, "tool_call_id")))
        if tool is None or tool["request_id"] != payload["request_id"]:
            raise ValueError("unknown or mismatched tool")
        return tool

    @staticmethod
    def _identifier(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"invalid {key}")
        return value

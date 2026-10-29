"""JSONL v4 事件校验与状态投影；诊断记录和有效模型历史分别归并。"""

import json
from itertools import groupby
from typing import Any

from adam.contracts.history import HistoryTurn
from adam.contracts.memory import validate_selection
from adam.contracts.protocol import validate_turn
from adam.session.compaction_log import CompactionLog
from adam.session.edit_log import EditLog
from adam.session.outcome import outcome_details


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


def parse_log(raw: bytes, session_id: str, *, allow_pending: bool = False) -> "SessionLog":
    """校验 v4 及关联；allow_pending 仅供持锁恢复读取未提交消息，不加入有效历史。"""
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
    log.extend(entries[1:], allow_pending=allow_pending)
    return log


def prepare_recovery(raw: bytes, session_id: str) -> tuple[bytes, "SessionLog"]:
    """规划尾部恢复，不访问文件；完整前缀有损坏时拒绝，不能借恢复吞掉非法事件。

    只允许清理未换行且 JSON/UTF-8 不完整的尾行；完整 JSON 缺换行则补齐后校验。
    半个成功消息块按合法前缀校验，完整行仍保留，后续由恢复事件关闭而非进入历史。
    """
    complete = raw
    if raw and not raw.endswith(b"\n"):
        boundary = raw.rfind(b"\n") + 1
        try:
            json.loads(raw[boundary:])
        except (ValueError, UnicodeError):
            complete = raw[:boundary]
        else:
            complete = raw + b"\n"
    return complete, parse_log(complete, session_id, allow_pending=True)


class SessionLog:
    """一次读取的事件投影；不访问文件，只依照明确的恢复事件关闭遗留操作。

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
        self.compactions = CompactionLog()
        self.edits = EditLog()
        self.tokens = 0
        self._unknown_usage = False
        self._pending_turn = ""
        self._pending_messages: list[dict[str, Any]] = []

    @property
    def unknown_usage(self) -> bool:
        """未知用量或未结算请求均阻止继续消费，不将缺失记录解释为零。"""
        return (
            self._unknown_usage
            or self.compactions.unknown_usage
            or any(not request["settled"] for request in self.requests.values())
        )

    def extend(self, entries: list[dict[str, Any]], *, allow_pending: bool = False) -> None:
        """逐条验证关联并更新投影；常规追加不能留下半个成功消息块。

        未完成 turn_started/tool_started 属于诊断，不加入待提交消息，因此允许重开读取。
        调用方仅在本方法成功后将整个批次写入文件；失败时丢弃此投影。
        allow_pending 仅用于恢复规划；未提交消息仍须通过结构及关联校验。
        """
        handlers = {
            "compaction_started": self._apply_compaction,
            "compaction_usage": self._apply_compaction,
            "compaction_completed": self._apply_compaction,
            "compaction_finished": self._apply_compaction,
            "turn_started": self._start_turn,
            "request_started": self._apply_request,
            "request_usage": self._apply_request,
            "request_finished": self._finish_request,
            "turn_finished": self._finish_turn,
            "tool_started": self._apply_tool,
            "tool_finished": self._apply_tool,
            "edit_planned": self._apply_edit,
            "edit_started": self._apply_edit,
            "edit_finished": self._apply_edit,
            "command_started": self._apply_command,
            "command_finished": self._apply_command,
            "command_denied": self._apply_command,
            "message": self._apply_message,
            "turn_completed": self._complete_turn,
            "session_recovered": self._recover,
        }
        for entry in entries:
            kind, payload = entry["type"], entry["payload"]
            if kind not in handlers:
                raise ValueError("unknown event")
            if self._pending_messages and kind not in {
                "message",
                "turn_completed",
                "session_recovered",
            }:
                raise ValueError("interleaved message block")
            handlers[kind](kind, payload)
            self.events.append(entry)
        if self._pending_messages:
            if not allow_pending:
                raise ValueError("incomplete turn")
            self._validate_pending(complete=False)

    def history_turns(self) -> list[HistoryTurn]:
        """按完整提交边界分组；仅已验证的成功消息成为候选上下文。"""
        return [
            HistoryTurn(turn_id, [message for _, message in items])
            for turn_id, items in groupby(self.messages, key=lambda item: item[0])
        ]

    def recovery_targets(self) -> dict[str, Any]:
        """返回尚无结束记录的操作；已记录失败的轮次不被恢复改成中断。"""
        return {
            "turn_ids": [
                key for key, value in self.turns.items() if value["status"] == "in_progress"
            ],
            "request_ids": [
                key for key, value in self.requests.items() if value["status"] == "in_progress"
            ],
            "tool_keys": [
                list(key) for key, value in self.tools.items() if value["status"] == "in_progress"
            ],
            "uncommitted_messages": len(self._pending_messages),
            **({"edit_ids": self.edits.pending_ids()} if self.edits.pending_ids() else {}),
            **(
                {"compaction_ids": self.compactions.pending_ids()}
                if self.compactions.pending_ids()
                else {}
            ),
        }

    def _recover(self, kind: str, payload: dict[str, Any]) -> None:
        """回放持锁恢复结果；不猜请求用量、命令执行结果，不提交残留消息。"""
        targets = self.recovery_targets()
        if any(payload.get(key) != value for key, value in targets.items()):
            raise ValueError("mismatched recovery targets")
        if payload.get("status") != "interrupted" or payload.get("reason") != "restart_recovery":
            raise ValueError("invalid recovery outcome")
        details = outcome_details(payload, "interrupted")
        if details["duration_ms"] is not None:
            raise ValueError("recovery duration is unknown")
        if self._pending_messages:
            self._validate_pending(complete=False)
        for turn_id in targets["turn_ids"]:
            self.turns[turn_id].update(details)
        for request_id in targets["request_ids"]:
            self.requests[request_id].update(details)
        for turn_id, call_id in targets["tool_keys"]:
            self.tools[(turn_id, call_id)].update(details)
        self.compactions.recover(details)
        self.edits.recover()
        # 完整物理记录留在 events；无提交标记的消息不能成为成功历史。
        self._pending_messages = []
        self._pending_turn = ""

    def _apply_compaction(self, kind: str, payload: dict[str, Any]) -> None:
        """摘要使用独立操作，不伪造聊天；已知消费并入会话累计值。"""
        owner = payload.get("turn_id")
        if kind == "compaction_started" and owner is not None:
            self._require_turn(payload)
        if kind == "compaction_started":
            self._validate_pruning(
                payload.get("pruned_tools", []), payload.get("source_turn_ids", [])
            )
        before = self.compactions.tokens
        self.compactions.apply(kind, payload, [turn.turn_id for turn in self.history_turns()])
        self.tokens += self.compactions.tokens - before

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
            context = payload.get("context")
            if context is not None:
                self._validate_context(context)
            self.requests[request_id] = {
                **({"context": context} if context is not None else {}),
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
        request["usage"] = (inputs, outputs)

    def _validate_context(self, context: object) -> None:
        """校验摘要覆盖前缀与未覆盖历史的连续后缀；旧请求可没有选择字段。"""
        if not isinstance(context, dict) or context.get("strategy") != "recent_turns_v1":
            raise ValueError("invalid context strategy")
        selected, omitted = context.get("selected_turn_ids"), context.get("omitted_turn_ids")
        if not isinstance(selected, list) or not isinstance(omitted, list):
            raise ValueError("invalid context ranges")
        summary = self.compactions.summary
        summary_id = context.get("summary_id")
        covered = list(summary.covered_turn_ids) if summary_id is not None and summary else []
        if summary_id is not None and (summary is None or summary.summary_id != summary_id):
            raise ValueError("invalid context summary")
        if covered + omitted + selected != [turn.turn_id for turn in self.history_turns()]:
            raise ValueError("invalid context sources")
        if "memory" in context:
            validate_selection(context["memory"])
        self._validate_pruning(context.get("pruned_tools", []), selected)
        for key in ("estimated_input_tokens", "original_history_tokens"):
            if type(context.get(key)) is not int or context[key] < 0:
                raise ValueError("invalid context estimate")

    def _validate_pruning(self, changes: object, selected: list[str]) -> None:
        """缩减来源必须对应本次素材中的真实完整工具结果；也供摘要素材校验使用。"""
        if not isinstance(changes, list):
            raise ValueError("invalid pruning records")
        seen = set()
        for item in changes:
            if not isinstance(item, dict):
                raise ValueError("invalid pruning entry")
            source_turn, call_id = item.get("turn_id"), item.get("tool_call_id")
            if not isinstance(source_turn, str) or not isinstance(call_id, str):
                raise ValueError("invalid pruning source")
            key = (source_turn, call_id)
            tool = self.tools.get(key)
            if key in seen or key[0] not in selected or tool is None or not tool["finished"]:
                raise ValueError("unknown or duplicate pruned tool")
            seen.add(key)
            if item.get("algorithm") != "head_tail_v1":
                raise ValueError("invalid pruning algorithm")
            if any(
                type(item.get(field)) is not int or item[field] < 0
                for field in ("original_chars", "result_chars", "head_chars", "tail_chars")
            ):
                raise ValueError("invalid pruning sizes")
            if (
                item["original_chars"] != len(tool["output"])
                or item["result_chars"] >= item["original_chars"]
            ):
                raise ValueError("pruning must reduce output")

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
        if payload.get("reason") == "api_not_sent" and (
            status != "failed" or request.get("usage") != (0, 0)
        ):
            raise ValueError("unsent request must have failed with zero usage")
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
                "status": "in_progress",
                "execution_id": None,
                "edit_id": None,
            }
            return
        tool = self._require_tool(payload)
        if tool["finished"] or not isinstance(payload.get("output"), str):
            raise ValueError("invalid or duplicate tool finish")
        execution_id = tool["execution_id"]
        if execution_id is not None and self.executions[execution_id]["status"] == "unknown":
            raise ValueError("command has no final result")
        edit_id = tool.get("edit_id")
        if edit_id is not None and self.edits.operations[edit_id]["status"] in {
            "planned",
            "unknown",
        }:
            raise ValueError("edit has no final result")
        tool.update(output=payload["output"], finished=True, status="finished")

    def _apply_edit(self, kind: str, payload: dict[str, Any]) -> None:
        """编辑事件只能属于已开始的 replace_text；具体状态由 EditLog 校验。"""
        self._require_turn(payload)
        self._require_request(payload)
        tool = self._require_tool(payload)
        if tool["name"] != "replace_text" or tool["finished"]:
            raise ValueError("invalid edit parent")
        edit_id = self._identifier(payload, "edit_id")
        if kind == "edit_planned":
            if tool.get("edit_id") is not None:
                raise ValueError("duplicate edit for tool")
        elif tool.get("edit_id") != edit_id:
            raise ValueError("mismatched edit id")
        self.edits.apply(kind, payload)
        tool["edit_id"] = edit_id

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
        self._validate_pending(complete=True)
        self.messages.extend((turn_id, message) for message in self._pending_messages)
        self.turns[turn_id].update(details)
        self.turns[turn_id]["status"] = "succeeded"
        self._pending_messages = []
        self._pending_turn = ""

    def _validate_pending(self, *, complete: bool) -> None:
        """提交与恢复共用消息校验；恢复只允许缺少后缀，已出现的证据仍须一致。"""
        turn_id = self._pending_turn
        validate_turn(self._pending_messages, allow_partial=not complete)
        if self._pending_messages[0]["content"] != self.turns[turn_id]["user_text"]:
            raise ValueError("mismatched user input")
        requests = [item for item in self.requests.values() if item["turn_id"] == turn_id]
        # 离线导入可没有请求；发生过请求时，连接失败不能冒充产生了最终回答。
        if requests and (
            not any(item["status"] == "succeeded" for item in requests)
            or any(
                not item["settled"]
                or (item["status"] != "succeeded" and item.get("reason") != "api_not_sent")
                for item in requests
            )
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
        if complete and seen != set(turn_tools):
            raise ValueError("uncommitted tool trace")

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

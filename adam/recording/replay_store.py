"""独立重放会话：生产存储负责日志校验，生成事件与原轨迹比较，工具事件只作为录制事实导入。"""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from adam.contracts.execution import ToolBudget
from adam.recording.format import RecordingError
from adam.recording.replay import ReplayTape
from adam.session.log import decode_records
from adam.session.storage import SessionStore

TOOL_EVENTS = {
    "tool_started",
    "tool_finished",
    "artifact_saved",
    "hook_started",
    "hook_finished",
    "plan_updated",
    "feedback_recorded",
    "review_requested",
    "review_finished",
    "edit_planned",
    "edit_started",
    "edit_finished",
    "command_started",
    "command_finished",
    "command_denied",
}
OUTCOME_EVENTS = {
    "request_finished",
    "turn_finished",
    "turn_completed",
    "compaction_completed",
    "compaction_finished",
    "compaction_usage",
}


def comparable_event(event: dict[str, Any]) -> dict[str, Any]:
    """只略去新的时钟测量和受控错误说明；业务消息、来源、原因与用量仍逐字段比较。"""
    value = {"type": event["type"], "payload": deepcopy(event["payload"])}
    if event["type"] in OUTCOME_EVENTS:
        value["payload"].pop("duration_ms", None)
        error = value["payload"].get("error")
        if isinstance(error, dict):
            error.pop("message", None)
    return value


def encode_events(events: list[dict[str, Any]]) -> bytes:
    """保留事件原文语义，编码为供生产存储校验的完整 JSONL 块。"""
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode("utf-8")


class ReplayStore(SessionStore):
    """只在全新诊断目录写入；原会话和录制文件不作为 SessionStore 的写入目标。"""

    def __init__(self, root: Path, tape: ReplayTape) -> None:
        self.tape = tape
        self.offset = len(tape.recording.seed["session_events"])
        self.session_id = tape.recording.seed["session_id"]
        try:
            root.mkdir(parents=True, exist_ok=False)
            super().__init__(root, identity_provider=tape.identity)
            directory = self.root / "2000" / "01" / "01"
            directory.mkdir(parents=True)
            with (directory / f"{self.session_id}.jsonl").open("xb") as file:
                file.write(encode_events(tape.recording.seed["session_events"]))
                file.flush()
                os.fsync(file.fileno())
            self.select_session(self.session_id)
            if self.load_events(self.session_id) != tape.recording.seed["session_events"]:
                raise RecordingError("录制起点需要恢复，不能当作稳定的重放起点。")
        except BaseException:
            if hasattr(self, "_lock"):
                self.close()
            raise

    def _append(self, session_id: str, data: bytes) -> None:
        """比较真实流程即将保存的事件，再沿生产验证与持锁写入；不改写业务生成结果。"""
        events = decode_records(data)
        self.tape.compare(self.session_id, session_id, "$.session_id")
        for index, event in enumerate(events, self.offset):
            if index >= len(self.tape.session_events):
                if self.tape.recording.complete:
                    self.tape.fail(f"$.session_events[{index}]", "<end>", comparable_event(event))
                self.tape.stop_incomplete("录制没有这一步的会话事件，保留独立诊断前缀。")
            self.tape.compare(
                comparable_event(self.tape.session_events[index]),
                comparable_event(event),
                f"$.session_events[{index}]",
            )
        super()._append(session_id, data)
        self.offset += len(events)

    def at_boundary(self, sequence: int) -> None:
        """确认调用结束前的所有内部事件已经由真实流程生成。"""
        self.tape.compare(
            self.tape.session_offsets[sequence],
            self.offset,
            "$.session_event_count",
            sequence=sequence,
        )

    def apply_tool_events(self, sequence: int, budget: ToolBudget) -> None:
        """导入本批外部动作的已记录事实；额度仍用当前 ToolBudget 消费，不执行动作。"""
        end = self.tape.session_offsets[sequence]
        if end < self.offset:
            self.tape.fail("$.session_event_count", end, self.offset, sequence=sequence)
        events = self.tape.session_events[self.offset : end]
        for event in events:
            if event["type"] not in TOOL_EVENTS:
                self.tape.fail(
                    "$.tool_event.type", "tool-owned event", event["type"], sequence=sequence
                )
            if event["type"] == "tool_started":
                budget.consume(event["payload"]["name"])
        if events:
            self._append(self.session_id, encode_events(events))
        self.at_boundary(sequence)

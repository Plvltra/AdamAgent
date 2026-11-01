"""把合成样本放入独立会话供生产摘要规划读取；不执行历史里的工具或命令。"""

import json
import secrets
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import Any

from adam.config.settings import ContextConfig
from adam.contracts.history import HistoryTurn
from adam.contracts.models import Session
from adam.session.storage import SessionStore
from scripts.compaction_cases import CompactionCase, CompactionScenario, CompactionSuite

FORMAT = "compaction_evaluation_v1"
LIMITS = ContextConfig(
    max_input_tokens=30000,
    safety_margin_tokens=512,
    summary_max_output_tokens=1024,
    retain_recent_turns=2,
    auto_compact=False,
    prune_tool_results=False,
)
DEFAULT_CASES = ("config-location", "auth-cause")


def limits_record() -> dict[str, int]:
    """只记录本实验实际使用的规划限额，其他配置新增字段不改变旧报告格式。"""
    return {
        "max_input_tokens": LIMITS.max_input_tokens,
        "safety_margin_tokens": LIMITS.safety_margin_tokens,
        "summary_max_output_tokens": LIMITS.summary_max_output_tokens,
        "retain_recent_turns": LIMITS.retain_recent_turns,
    }


def digest(value: object) -> str:
    """对记录的逻辑 JSON 求摘要，避免换行或字段显示顺序影响来源核对。"""
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def schedule(suite: CompactionSuite, selected: tuple[str, ...]) -> list[dict[str, Any]]:
    """每个查询比较两组；交替先后顺序，同一查询不能重复计入。"""
    if not 1 <= len(selected) <= 8 or len(set(selected)) != len(selected):
        raise ValueError("选择 1～8 个不重复查询。")
    if not set(selected) <= {case.case_id for case in suite.cases}:
        raise ValueError("查询 ID 不存在。")
    return [
        {"index": 2 * number + offset + 1, "case_id": identity, "arm": arm}
        for number, identity in enumerate(selected)
        for offset, arm in enumerate(
            ("generic", "focused") if number % 2 == 0 else ("focused", "generic")
        )
    ]


def turn_identity(scenario_id: str, original: str) -> str:
    """固定合成轮次身份，使不同查询和压缩组引用同一原文。"""
    return sha256(f"{scenario_id}/{original}".encode()).hexdigest()[:32]


def mapped_scenario(scenario: CompactionScenario) -> CompactionScenario:
    """把数据中的易读别名转换为生产日志接受的 ID，消息内容保持相同。"""
    return replace(
        scenario,
        history=tuple(
            HistoryTurn(turn_identity(scenario.scenario_id, turn.turn_id), deepcopy(turn.messages))
            for turn in scenario.history
        ),
    )


def mapped_case(case: CompactionCase) -> CompactionCase:
    """判据引用使用与合成历史相同的 ID 映射，不把判据发送给摘要模型。"""
    return replace(
        case,
        checks=tuple(
            replace(
                check,
                source_ref=turn_identity(case.scenario_id, check.source_ref.split(":")[0])
                + ":"
                + check.source_ref.split(":")[1],
            )
            for check in case.checks
        ),
    )


def seed_scenario(store: SessionStore, scenario: CompactionScenario) -> Session:
    """导入合成已完成历史，返回独立会话；生成的工具轨迹只是评估资料，不代表本机执行。

    调用者必须传入新的评估专用存储。原数据不修改；fixture 消费为零，
    之后真实摘要请求的消费由评估运行记录单独统计。
    """
    if store.list_sessions():
        raise ValueError("合成历史只能写入新的评估存储。")
    sid = store.create_session("B05 合成历史，非真实执行记录", scenario.constraints)
    session = store.select_session(sid)
    original_provider = store.identity_provider
    try:
        for turn in mapped_scenario(scenario).history:
            store.identity_provider = lambda kind, size, identity=turn.turn_id: (
                identity if kind == "turn" else secrets.token_hex(size)
            )
            tid = store.start_turn(sid, turn.messages[0]["content"])
            request_id = ""
            for message in turn.messages[1:]:
                calls = message.get("tool_calls") or []
                if calls:
                    request_id = store.start_request(sid, tid, "synthetic-history")
                    store.finish_request(sid, tid, request_id, (0, 0))
                    for call in calls:
                        store.append_tool_event(
                            sid,
                            "tool_started",
                            {
                                "turn_id": tid,
                                "request_id": request_id,
                                "tool_call_id": call["id"],
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                            },
                        )
                if message["role"] == "tool":
                    store.append_tool_event(
                        sid,
                        "tool_finished",
                        {
                            "turn_id": tid,
                            "request_id": request_id,
                            "tool_call_id": message["tool_call_id"],
                            "output": message["content"],
                        },
                    )
            store.append_messages(sid, tid, deepcopy(turn.messages))
    finally:
        store.identity_provider = original_provider
    return session

"""知识发布的有限状态与审查格式；已发布表示用户采用，不表示事实经过证明。"""

import re
from datetime import datetime
from typing import Any, Protocol

from adam.documents.models import DocumentError

TRANSITIONS = {
    "pending": {"reviewed", "changes_requested", "rejected"},
    "reviewed": {"reviewed", "changes_requested", "published", "rejected"},
    "changes_requested": {"reviewed", "changes_requested", "rejected"},
    "published": {"withdrawn", "needs_review"},
    "needs_review": {"withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}


class KnowledgeReviewer(Protocol):
    """宿主注入的独立审查者；只获得冻结候选，不能批准发布或修改知识状态。"""

    @property
    def identity(self) -> str: ...

    def review(self, candidate: dict[str, Any], review_id: str) -> dict[str, Any]:
        """返回意见；review_id 由宿主分配，用于关联独立请求记录，不代表成功。"""
        ...


def validate_review(value: object, expected_evidence: set[str] | None = None) -> dict[str, Any]:
    """只接受明确意见、反证处理及引用声明；回显 ID 不证明审查者理解了证据。"""
    if not isinstance(value, dict) or value.keys() != {
        "verdict",
        "summary",
        "counterevidence_assessment",
        "evidence_ids",
    }:
        raise DocumentError("知识审查格式无效，不能包含批准或发布状态。")
    if not isinstance(value["verdict"], str) or value["verdict"] not in {
        "accept",
        "revise",
        "reject",
    }:
        raise DocumentError("知识审查 verdict 必须是 accept、revise 或 reject。")
    for name in ("summary", "counterevidence_assessment"):
        validate_text(value[name], maximum=2000)
    identifiers = value["evidence_ids"]
    if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 16:
        raise DocumentError("知识审查须声明 1～16 个已提供的证据 ID。")
    for identity in identifiers:
        validate_hex(identity, 16)
    if len(set(identifiers)) != len(identifiers) or (
        expected_evidence is not None and set(identifiers) != expected_evidence
    ):
        raise DocumentError("知识审查的引用声明没有覆盖全部支持和反证，或包含未知/重复 ID。")
    return value


def initial_state(candidate_id: str, candidate_hash: str) -> dict[str, Any]:
    """冻结候选的初始状态；没有状态事件不能被解释为通过审查。"""
    return {
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "revision": 0,
        "status": "pending",
        "review": None,
        "reviewer": None,
        "review_id": None,
        "reason": "尚未独立审查",
        "at": None,
    }


def fold_events(value: object, scope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """严格重放有界状态链；非法跳跃、版本断链或模型自报批准都不能成为已发布状态。"""
    if (
        not isinstance(value, dict)
        or value.keys() != {"format", "scope", "events"}
        or (value["format"] != "knowledge_states_v1" or value["scope"] != scope)
    ):
        raise DocumentError("知识状态文件结构或范围无效。")
    events = value["events"]
    if not isinstance(events, list) or len(events) > 4096:
        raise DocumentError("知识状态历史超过上限或格式无效。")
    current: dict[str, dict[str, Any]] = {}
    review_ids = set()
    for event in events:
        _validate_event(event)
        identity = event["candidate_id"]
        previous = current.get(identity, initial_state(identity, event["candidate_hash"]))
        if (
            event["candidate_hash"] != previous["candidate_hash"]
            or event["revision"] != previous["revision"] + 1
            or event["status"] not in TRANSITIONS[previous["status"]]
        ):
            raise DocumentError("知识状态版本或转换无效，不能跳过审查或重新发布已撤回候选。")
        if event["status"] in {"reviewed", "changes_requested"}:
            if event["review_id"] in review_ids:
                raise DocumentError("知识审查请求 ID 重复。")
            review_ids.add(event["review_id"])
        elif any(event[name] != previous[name] for name in ("review", "review_id", "reviewer")):
            raise DocumentError("知识状态不能在批准或撤回时更换审查内容。")
        current[identity] = event
    return current


def validate_hex(value: object, width: int) -> None:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{width}}}", value) is None:
        raise DocumentError("知识状态身份格式无效。")


def validate_text(value: object, *, maximum: int = 2000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DocumentError(f"知识状态说明必须是 1～{maximum} 字符的非空文本。")


def _validate_event(event: object) -> None:
    """区分模型意见、用户决定和来源失效；这些来源由宿主填写，不取模型字段。"""
    if not isinstance(event, dict) or event.keys() != {
        "candidate_id",
        "candidate_hash",
        "revision",
        "status",
        "review",
        "reviewer",
        "review_id",
        "reason",
        "at",
        "decision_source",
        "observed_generation",
    }:
        raise DocumentError("知识状态事件格式无效。")
    validate_hex(event["candidate_id"], 8)
    validate_hex(event["candidate_hash"], 64)
    validate_text(event["reason"])
    if type(event["revision"]) is not int or not 1 <= event["revision"] <= 4096:
        raise DocumentError("知识状态修订号无效。")
    if not isinstance(event["status"], str) or event["status"] not in TRANSITIONS:
        raise DocumentError("知识状态名称无效。")
    try:
        if not isinstance(event["at"], str) or len(event["at"]) > 64:
            raise ValueError("invalid timestamp")
        at = datetime.fromisoformat(event["at"])
        if at.utcoffset() is None:
            raise ValueError("timezone missing")
    except (TypeError, ValueError) as error:
        raise DocumentError("知识状态时间无效。") from error
    if event["observed_generation"] is not None:
        validate_hex(event["observed_generation"], 64)
    expected_source = (
        "reviewer"
        if event["status"] in {"reviewed", "changes_requested"}
        else ("source_change" if event["status"] == "needs_review" else "user")
    )
    if event["decision_source"] != expected_source:
        raise DocumentError("知识状态的决定来源与动作不一致。")
    if event["review"] is not None:
        validate_review(event["review"])
        validate_text(event["reviewer"], maximum=200)
        validate_hex(event["review_id"], 16)
    elif event["reviewer"] is not None or event["review_id"] is not None:
        raise DocumentError("知识审查身份与内容没有配对。")
    if event["status"] == "reviewed" and (
        event["review"] is None or event["review"]["verdict"] != "accept"
    ):
        raise DocumentError("知识审查未接受，不能标记为通过。")
    if event["status"] == "changes_requested" and (
        event["review"] is None or event["review"]["verdict"] == "accept"
    ):
        raise DocumentError("知识审查修改状态与意见不一致。")

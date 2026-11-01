"""确定性的离线记忆整理；只保存候选，未经确认不会改变任何记忆版本。"""

import hashlib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryRecord, decode_record, timestamp, validate_record
from adam.contracts.memory_organization import MemoryCandidate, MemoryOrganization
from adam.contracts.models import valid_session_id
from adam.memory.retrieval import keywords

MAX_RECORDS = 256
MAX_PROPOSAL_BYTES = 8 * 1024 * 1024


def propose(
    records: list[MemoryRecord], project: str | None, now: datetime, *, identity: str | None = None
) -> MemoryOrganization:
    """给一个范围的最新未删除记录分组；不做语义推理，不裁掉超限输入。

    相同主体的不同表述，或无主体但词语高度重合的记录，仅列为可能冲突。
    未来生效记录保留，过期记录单列；候选记录覆盖输入且不跨范围或混合同名异人。
    """
    if now.tzinfo is None or len(records) > MAX_RECORDS:
        raise MemoryInputError("整理需要带时区的时间，且当前范围最多 256 条记忆。")
    try:
        for record in records:
            validate_record(record)
            if record.deleted or record.project != project:
                raise ValueError("invalid organization scope")
        if len({record.memory_id for record in records}) != len(records):
            raise ValueError("duplicate source identity")
    except (ValueError, TypeError, AttributeError):
        raise MemoryInputError("整理来源的范围、身份或版本无效。") from None
    ordered = sorted(records, key=lambda record: record.memory_id)
    candidates = []
    active = []
    for record in ordered:
        if record.expires_at is not None and timestamp(record.expires_at) <= now:
            candidates.append(_candidate("expired", "已到期；保留历史，是否清理需确认。", [record]))
        elif not record.active(now):
            candidates.append(_candidate("keep", "尚未生效，保留原有效期。", [record]))
        else:
            active.append(record)
    for group in _groups(active):
        if len(group) == 1:
            kind, reason = "keep", "没有发现同组候选，保留原记录。"
        elif len({(r.content, r.valid_from, r.expires_at) for r in group}) == 1:
            kind, reason = "merge", "正文与有效期相同，可确认合并；所有旧来源仍须保留。"
        else:
            kind, reason = "conflict", "同一主体或词语相近但表述/有效期不同，需核对；未判定谁正确。"
        candidates.append(_candidate(kind, reason, group))
    return MemoryOrganization(
        identity or secrets.token_hex(4),
        now.isoformat(),
        project,
        tuple(sorted(candidates, key=lambda candidate: (candidate.kind, candidate.candidate_id))),
    )


def save_organization(root: Path, organization: MemoryOrganization) -> None:
    """独占创建完整候选文件并刷盘；失败不触碰记忆库，残缺文件不能作为有效候选读取。"""
    path = _path(root, organization.organization_id)
    payload = json.dumps(organization.to_dict(), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_PROPOSAL_BYTES:
        raise MemoryInputError("整理候选过大，本次未保存。")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise MemoryStoreError("整理候选保存失败，原记忆未修改。") from error


def load_organization(root: Path, identity: str, project: str | None) -> MemoryOrganization:
    """读取冻结候选，校验分类和来源快照一致；不使用今日记忆重造旧候选。"""
    path = _path(root, identity)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PROPOSAL_BYTES + 1)
        if len(raw) > MAX_PROPOSAL_BYTES:
            raise ValueError("oversized proposal")
        value = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("project") != project
        ):
            raise ValueError("invalid proposal scope")
        candidates = value["candidates"]
        if not isinstance(candidates, list) or len(candidates) > MAX_RECORDS:
            raise ValueError("invalid candidate count")
        records = [decode_record(record) for group in candidates for record in group["records"]]
        actual = propose(records, project, timestamp(value["created_at"]), identity=identity)
        if actual.to_dict() != value:
            raise ValueError("altered candidate classification")
        return actual
    except (OSError, ValueError, TypeError, KeyError, AttributeError, MemoryInputError) as error:
        raise MemoryStoreError(
            "整理候选不存在、损坏或不属于当前范围；原文件和记忆保留。"
        ) from error


def _path(root: Path, identity: str) -> Path:
    """只接受短 ID，不允许把查询或保存路径指向候选目录之外。"""
    if not isinstance(identity, str) or not valid_session_id(identity):
        raise MemoryInputError("整理候选 ID 无效。")
    return root / "organizations" / f"{identity}.json"


def _candidate(kind: str, reason: str, records: list[MemoryRecord]) -> MemoryCandidate:
    """同样的分类和确切来源得到相同候选 ID；正文仍随完整快照保存。"""
    source = [(record.memory_id, record.revision) for record in records]
    key = json.dumps([kind, source], separators=(",", ":")).encode("utf-8")
    return MemoryCandidate(hashlib.sha256(key).hexdigest()[:12], kind, reason, tuple(records))


def _groups(records: list[MemoryRecord]) -> list[list[MemoryRecord]]:
    """按显式主体或无主体文本重合分组；相似度只提示待核对，不产生事实合并。"""
    groups: list[list[MemoryRecord]] = []
    for record in records:
        matched = [group for group in groups if any(_related(record, other) for other in group)]
        merged = sorted(
            [record, *(item for group in matched for item in group)], key=lambda r: r.memory_id
        )
        groups = [group for group in groups if group not in matched]
        groups.append(merged)
    return groups


def _related(first: MemoryRecord, second: MemoryRecord) -> bool:
    """显式不同主体永不串联；无主体仅按保守的词集合重合提示。"""
    if first.subject is not None or second.subject is not None:
        return bool(first.subject and second.subject and first.subject.key == second.subject.key)
    if first.content == second.content:
        return True
    left, right = keywords(first.content), keywords(second.content)
    return bool(left and right and len(left & right) / len(left | right) >= 0.6)

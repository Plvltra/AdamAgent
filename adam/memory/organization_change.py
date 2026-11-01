"""整理确认与撤销的纯变更构造；调用方持同一存储锁验证并提交整批版本。"""

import secrets
from dataclasses import dataclass, replace
from datetime import datetime

from adam.contracts.errors import MemoryInputError
from adam.contracts.memory import (
    MemoryDerivation,
    MemoryRecord,
    MemoryReference,
    MemorySource,
    timestamp,
    validate_record,
)
from adam.contracts.memory_organization import MemoryCandidate


@dataclass(frozen=True)
class OrganizationChange:
    """result 与 preceding 必须原子保存；already_saved 表示只返回既有结果。"""

    result: MemoryRecord
    preceding: tuple[MemoryRecord, ...] = ()
    already_saved: bool = False


def consolidate(
    history: list[MemoryRecord],
    candidate: MemoryCandidate,
    organization_id: str,
    project: str | None,
    source: MemorySource,
    now: datetime,
    content: str | None,
) -> OrganizationChange:
    """仅采纳合并或经用户提供正文的冲突候选；来源变化或到期时整组拒绝。

    结果是一条带完整引用链的稳定概览，原记录在确认时到期而非被删除。
    不延长来源有效期，重复确认相同候选返回既有记录，不恢复被用户删除的结果。
    """
    if candidate.kind not in {"merge", "conflict"} or len(candidate.records) < 2:
        raise MemoryInputError("只能采纳合并或冲突候选；保留与过期条目无需生成新概览。")
    if content is None:
        if candidate.kind != "merge":
            raise MemoryInputError("冲突候选须用 -- 明确提供整理后的正文，程序不替你决定事实。")
        if len({(r.content, r.valid_from, r.expires_at) for r in candidate.records}) != 1:
            raise MemoryInputError("合并候选的正文或有效期不一致。")
        content = candidate.records[0].content
    references = tuple(MemoryReference(r.memory_id, r.revision) for r in candidate.records)
    origin = MemoryDerivation(organization_id, candidate.candidate_id, references)
    initial = next(
        (r for r in history if r.revision == 1 and r.derivation == origin and r.project == project),
        None,
    )
    latest = {r.memory_id: r for r in history}
    if initial is not None:
        if initial.content != content:
            raise MemoryInputError("该候选已处理，不能用同一候选覆盖先前接受的正文。")
        return OrganizationChange(latest[initial.memory_id], already_saved=True)
    if any(
        r.project != project or latest.get(r.memory_id) != r or not r.active(now)
        for r in candidate.records
    ):
        raise MemoryInputError("整理来源已变化、删除或到期；请重新生成候选，原记忆未修改。")
    expiration = min(
        (timestamp(r.expires_at) for r in candidate.records if r.expires_at), default=None
    )
    result = MemoryRecord(
        _new_id(latest),
        1,
        content,
        project,
        now.isoformat(),
        now.isoformat(),
        replace(source, operation="add"),
        expiration.isoformat() if expiration else None,
        subject=candidate.records[0].subject,
        valid_from=now.isoformat(),
        derivation=origin,
    )
    try:
        validate_record(result)
    except (ValueError, TypeError, AttributeError):
        raise MemoryInputError("整理正文、主体或来源无效，未保存概览。") from None
    ended = tuple(
        replace(
            record,
            revision=record.revision + 1,
            updated_at=now.isoformat(),
            expires_at=now.isoformat(),
            source=replace(source, operation="update"),
        )
        for record in candidate.records
    )
    return OrganizationChange(result, ended)


def undo_consolidation(
    history: list[MemoryRecord], result: MemoryRecord, source: MemorySource, now: datetime
) -> OrganizationChange:
    """撤销未再修改的整理结果，并原子恢复各来源的原有效期；不恢复原本已到期事实。

    任一来源在整理后被修改，或结果本身不是最初版本，就拒绝整组撤销。
    仅追加版本，不抹去接受与撤销发生过的证据。
    """
    origin = result.derivation
    if origin is None or result.revision != 1 or result.deleted:
        raise MemoryInputError("只能撤销尚未修改或删除的整理概览。")
    latest = {r.memory_id: r for r in history}
    originals = {(r.memory_id, r.revision): r for r in history}
    restored = []
    for reference in origin.sources:
        original = originals.get((reference.memory_id, reference.revision))
        if original is None:
            raise MemoryInputError("整理来源缺失，不能撤销。")
        ended = replace(
            original,
            revision=original.revision + 1,
            updated_at=result.created_at,
            expires_at=result.valid_from,
            source=replace(result.source, operation="update"),
        )
        if latest.get(original.memory_id) != ended:
            raise MemoryInputError("原记忆在整理后又被修改，拒绝覆盖；本次未撤销。")
        restored.append(
            replace(
                original,
                revision=ended.revision + 1,
                updated_at=now.isoformat(),
                source=replace(source, operation="update"),
            )
        )
    removed = replace(
        result,
        revision=2,
        deleted=True,
        updated_at=now.isoformat(),
        source=replace(source, operation="delete"),
    )
    return OrganizationChange(removed, tuple(restored))


def _new_id(records: dict[str, MemoryRecord]) -> str:
    """新概览必须取得独立身份，不覆盖任意历史记忆 ID。"""
    while True:
        identity = secrets.token_hex(4)
        if identity not in records:
            return identity

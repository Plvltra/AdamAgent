"""离线记忆整理候选：冻结来源版本，分类本身不代表已确认的事实。"""

from dataclasses import dataclass
from typing import Any

from adam.contracts.memory import MemoryRecord


@dataclass(frozen=True)
class MemoryCandidate:
    """一组待核对记录；merge 只建议合并同文，conflict 只表示需要核对。"""

    candidate_id: str
    kind: str
    reason: str
    records: tuple[MemoryRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        """保存完整原文、主体与确切版本，后续确认不能只凭候选 ID 修改最新记录。"""
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "reason": self.reason,
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True)
class MemoryOrganization:
    """一个范围的冻结建议与拒绝状态；保存候选不改变原记忆或模型上下文。"""

    organization_id: str
    created_at: str
    project: str | None
    candidates: tuple[MemoryCandidate, ...]
    rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        """输出版本化候选快照；事实有效性以 created_at 的观察时刻为准。"""
        value = {
            "version": 1,
            "organization_id": self.organization_id,
            "created_at": self.created_at,
            "project": self.project,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
        if self.rejected:
            value["rejected"] = True
        return value

"""知识候选的证据校验和冻结保存；保存不等于审查、批准或发布。"""

import copy
import re
import secrets
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.documents.evidence_audit import audit_summary, validate_summary
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot

MAX_CANDIDATES = 256
MAX_CANDIDATE_BYTES = 1024 * 1024


class KnowledgeProposals:
    """为固定资料范围保存独立候选；原文、现有记忆及已发布知识都不由此接口修改。"""

    def __init__(self, root: Path, index: DocumentIndex) -> None:
        """root 由宿主提供，按工作目录及资料范围隔离；候选 ID 和状态不接受模型指定。"""
        self.index = index
        self.scope = copy.deepcopy(index.scope)
        self.root = root.resolve() / digest(self.scope)[:24]
        self.directory = self.root / "candidates"
        self.lock = FileLock(str(self.root / "knowledge.lock"), timeout=5)

    def create(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """核对当前支持与反证，原子保存待审候选并返回独立副本。

        proposal 只包含 statement、applicability、reasoning、valid_from/valid_until、
        support、counterevidence 与 source_generation；引用格式复用来源审计契约。
        允许反证为空，但支持证据必须存在。来源有效不证明提炼结论或时间判断正确。
        """
        frozen = copy.deepcopy(proposal)
        summary = validate_proposal(frozen)
        self._current_scope()
        self.index.check_cancel()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.lock:
                if (
                    len(list(islice(self.directory.glob("*.json"), MAX_CANDIDATES)))
                    >= MAX_CANDIDATES
                ):
                    raise DocumentError("知识候选数量达到上限，请归档已处理候选后再创建。")
                audit = audit_summary(self.index, summary)
                if audit["status"] != "provenance_valid":
                    raise DocumentError("知识候选的支持或反证缺失、错绑或已过时，未保存。")
                identity = self._unused_id()
                candidate = {
                    "format": "knowledge_candidate_v1",
                    "candidate_id": identity,
                    "created_at": datetime.now(UTC).isoformat(),
                    "scope": self.scope,
                    "status": "pending",
                    "semantic_status": "unreviewed",
                    "proposal": frozen,
                }
                candidate["content_hash"] = digest(candidate)
                self.index.check_cancel()
                save_snapshot(self._path(identity), candidate)
                return copy.deepcopy(candidate)
        except Timeout as error:
            raise DocumentError("知识候选正在被其他进程使用，请稍后重试。") from error
        except OSError as error:
            raise DocumentError("知识候选保存失败；未发布内容，原候选和源资料保留。") from error

    def read(self, candidate_id: str) -> dict[str, Any]:
        """读取当时冻结的待审提案，不用当前原文重写旧证据，也不宣称证据仍有效。"""
        path = self._path(candidate_id)
        self.index.check_cancel()
        try:
            if not path.exists():
                raise DocumentError("知识候选不存在或不属于当前资料范围。")
            with self.lock:
                if path.stat().st_size > MAX_CANDIDATE_BYTES:
                    raise DocumentError("知识候选文件过大，保留原文件，未加载。")
                value = load_snapshot(path)
                return self._decode(value, candidate_id)
        except Timeout as error:
            raise DocumentError("知识候选正在被其他进程使用，请稍后重试。") from error
        except OSError as error:
            raise DocumentError("知识候选读取失败，请检查路径和权限；原文件保留。") from error
        except DocumentError as error:
            # 候选是需要保留的审查记录；不能沿用索引缓存“移走后重建”的恢复提示。
            raise DocumentError(
                "知识候选不存在、损坏或不属于当前范围；保留原文件，未发布。"
            ) from error

    def _path(self, candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or re.fullmatch(r"[0-9a-f]{8}", candidate_id) is None:
            raise DocumentError("知识候选 ID 必须是 8 位十六进制字符。")
        return self.directory / f"{candidate_id}.json"

    def _unused_id(self) -> str:
        """在持锁区间分配短 ID；碰撞有限重试，绝不覆盖已有候选。"""
        for _ in range(4):
            identity = secrets.token_hex(4)
            if not self._path(identity).exists():
                return identity
        raise DocumentError("知识候选 ID 连续冲突，原候选保留，请稍后重试。")

    def _current_scope(self) -> None:
        if self.index.scope != self.scope:
            raise DocumentError("知识资料范围已变化，请重新建立候选服务。")

    def _decode(self, value: object, identity: str) -> dict[str, Any]:
        """核对身份、格式、内容摘要与冻结提案；不执行当前来源校验或状态恢复。"""
        if not isinstance(value, dict) or value.keys() != {
            "format",
            "candidate_id",
            "created_at",
            "scope",
            "status",
            "semantic_status",
            "proposal",
            "content_hash",
        }:
            raise DocumentError("知识候选结构无效。")
        if (
            value["format"] != "knowledge_candidate_v1"
            or value["candidate_id"] != identity
            or value["scope"] != self.scope
            or value["status"] != "pending"
            or value["semantic_status"] != "unreviewed"
            or value["content_hash"]
            != digest({k: v for k, v in value.items() if k != "content_hash"})
        ):
            raise DocumentError("知识候选身份、状态或内容摘要无效。")
        _timestamp(value["created_at"], optional=False)
        validate_proposal(value["proposal"])
        self.index.check_cancel()
        return copy.deepcopy(value)


def validate_proposal(proposal: object) -> dict[str, Any]:
    """校验候选正文、适用时间和两类引用，返回可交给来源审计的载荷，不读取文件。"""
    if not isinstance(proposal, dict) or proposal.keys() != {
        "statement",
        "applicability",
        "reasoning",
        "valid_from",
        "valid_until",
        "support",
        "counterevidence",
        "source_generation",
    }:
        raise DocumentError("知识候选字段无效，不能包含自报批准或发布状态。")
    for name, maximum in (("statement", 2000), ("applicability", 1000), ("reasoning", 2000)):
        text = proposal[name]
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise DocumentError(f"知识候选 {name} 必须是 1～{maximum} 字符的非空文本。")
    start = _timestamp(proposal["valid_from"])
    end = _timestamp(proposal["valid_until"])
    if start is not None and end is not None and start >= end:
        raise DocumentError("知识候选结束时间必须晚于开始时间。")
    if not isinstance(proposal["support"], list) or not proposal["support"]:
        raise DocumentError("知识候选至少需要一条支持证据。")
    if not isinstance(proposal["counterevidence"], list):
        raise DocumentError("知识候选反证必须是数组；没有已知反证时显式使用空数组。")
    claims = [{"text": proposal["statement"], "citations": proposal["support"]}]
    if proposal["counterevidence"]:
        claims.append(
            {"text": "待核对的反证，不代表已推翻候选", "citations": proposal["counterevidence"]}
        )
    summary = {"source_generation": proposal["source_generation"], "claims": claims}
    validate_summary(summary)
    support_ids = {(c["chunk_id"], c["start"], c["end"]) for c in proposal["support"]}
    if support_ids & {(c["chunk_id"], c["start"], c["end"]) for c in proposal["counterevidence"]}:
        raise DocumentError("同一原文范围不能同时登记为支持和反证，请明确各自依据。")
    return summary


def _timestamp(value: object, *, optional: bool = True) -> datetime | None:
    """有效期允许未知，但有值时必须包含时区；时间由候选提出，来源审计不替它背书。"""
    if value is None and optional:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("timestamp text")
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError("timezone missing")
        return result
    except ValueError as error:
        raise DocumentError("知识候选时间必须是带时区的 ISO 日期时间，未知时使用 null。") from error

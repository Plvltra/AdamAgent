"""在已授权记忆快照上做本地语义排序；失败返回确定的关键词结果。"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from threading import RLock

from adam.contracts.embedding import EmbeddingBackend, unit_vector
from adam.contracts.memory import MemoryRecord
from adam.memory.retrieval import rank_records


@dataclass(frozen=True)
class MemoryRanking:
    """实际排序来源；scores 是相似度观察，不是事实正确率或权限。"""

    records: tuple[MemoryRecord, ...]
    strategy: str
    embedding: str | None = None
    fallback_reason: str | None = None
    scores: tuple[tuple[str, float], ...] = ()


class SemanticMemoryRanker:
    """复用本地编码器，在内存中缓存有限个已授权条目的向量。

    不扫描记忆库、不持久化向量，也不改变记录。调用方先过滤项目和全局范围；
    本类再次排除删除及过期条目。模型、内容或版本变化会使缓存失效。
    """

    def __init__(
        self,
        backend: EmbeddingBackend,
        *,
        min_score: float = 0.5,
        max_records: int = 256,
    ) -> None:
        """注入已配置的本地后端；数量上限约束一次推理和内存缓存，不裁掉候选后假称全量。"""
        if type(max_records) is not int or not 1 <= max_records <= 1024:
            raise ValueError("invalid semantic memory limit")
        if type(min_score) not in {int, float} or not 0 <= min_score <= 1:
            raise ValueError("invalid semantic memory threshold")
        self.backend = backend
        self.min_score = min_score
        self.max_records = max_records
        self._identity: str | None = None
        self._cache: dict[str, list[float]] = {}
        self._lock = RLock()

    def rank(self, records: list[MemoryRecord], query: str, now: datetime) -> MemoryRanking:
        """排序当前有效快照；模型不可用、输入超限或坏向量时整批降级，不返回部分假成功。

        KeyboardInterrupt 等取消不吞掉；降级原因使用固定码，不复制后端异常中的内容。
        """
        active = [record for record in records if record.active(now)]
        if not active:
            return MemoryRanking((), "keyword_v1")
        reason = (
            "candidate_limit"
            if len(active) > self.max_records
            else "query_limit"
            if len(query) > 1000
            else None
        )
        if reason is None:
            with self._lock:
                try:
                    return self._rank(active, query)
                except Exception:
                    # 编码器是外部依赖边界，不能把推理失败伪装成没有相关记忆。
                    self._cache.clear()
                    self._identity = None
                    reason = "embedding_unavailable"
        return MemoryRanking(
            tuple(rank_records(active, query, now)), "keyword_fallback_v1", fallback_reason=reason
        )

    def _rank(self, records: list[MemoryRecord], query: str) -> MemoryRanking:
        """锁内检查模型版本及完整向量批次；全部有效后才替换缓存和排序。"""
        identity = self.backend.identity
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 512:
            raise ValueError("invalid embedding identity")
        if identity != self._identity:
            self._cache.clear()
        keys = [self._key(record) for record in records]
        cached = {key: self._cache[key] for key in keys if key in self._cache}
        missing = [
            (key, record) for key, record in zip(keys, records, strict=True) if key not in cached
        ]
        if missing:
            vectors = self.backend.passages([record.content for _, record in missing])
            if not isinstance(vectors, list) or len(vectors) != len(missing):
                raise ValueError("incomplete memory vectors")
            cached.update(
                (key, unit_vector(vector))
                for (key, _), vector in zip(missing, vectors, strict=True)
            )
        query_vector = unit_vector(self.backend.query(query))
        if any(len(vector) != len(query_vector) for vector in cached.values()):
            raise ValueError("incompatible memory vectors")
        self._identity, self._cache = identity, cached
        scored = [
            (
                max(
                    -1.0,
                    min(1.0, sum(a * b for a, b in zip(cached[key], query_vector, strict=True))),
                ),
                record,
            )
            for key, record in zip(keys, records, strict=True)
        ]
        scored.sort(key=lambda item: (item[0], item[1].updated_at, item[1].memory_id), reverse=True)
        matches = [(score, record) for score, record in scored if score >= self.min_score]
        return MemoryRanking(
            tuple(record for _, record in matches),
            "semantic_v1",
            identity,
            scores=tuple((record.memory_id, score) for score, record in matches),
        )

    @staticmethod
    def _key(record: MemoryRecord) -> str:
        """范围、ID、版本及正文共同决定缓存键；新版本不能复用旧内容向量。"""
        text = f"{record.project}\0{record.memory_id}\0{record.revision}\0{record.content}"
        return sha256(text.encode("utf-8")).hexdigest()

"""按片段身份合并 BM25 与向量召回，只融合排名，不混加不可比的原得分。"""

import math
from collections.abc import Sequence

from adam.documents.models import Chunk, DocumentError, RetrievalRank, SearchHit


def reciprocal_rank_fusion(
    keyword: Sequence[SearchHit], semantic: Sequence[SearchHit], limit: int, constant: int = 60
) -> list[SearchHit]:
    """按 1/(constant+rank) 相加排序，排名从 1 开始，保留原片段和每路得分。

    每路最多 20 个候选，同路重复 ID 只计算第一次且不占新名次。
    同一 ID 指向不同原文/版本时拒绝融合；空列表合法，同分按稳定来源排序。
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= 20
        or type(constant) is not int
        or not 1 <= constant <= 1000
        or len(keyword) > 20
        or len(semantic) > 20
    ):
        raise DocumentError("混合检索的结果数、候选数或 RRF 常量无效。")
    chunks: dict[str, Chunk] = {}
    ranks: dict[str, list[RetrievalRank]] = {}
    for method, hits in (("keyword", keyword), ("semantic", semantic)):
        seen: set[str] = set()
        for hit in hits:
            if type(hit.score) not in {int, float} or not math.isfinite(hit.score):
                raise DocumentError("召回列表包含无效分数。")
            identity = hit.chunk.chunk_id
            if identity in chunks and chunks[identity] != hit.chunk:
                raise DocumentError("同一片段 ID 的来源或版本不一致，未进行融合。")
            chunks[identity] = hit.chunk
            if identity in seen:
                continue
            seen.add(identity)
            ranks.setdefault(identity, []).append(RetrievalRank(method, len(seen), hit.score))
    combined = [
        SearchHit(
            chunks[identity], sum(1 / (constant + rank.rank) for rank in evidence), tuple(evidence)
        )
        for identity, evidence in ranks.items()
    ]
    return sorted(
        combined,
        key=lambda hit: (-hit.score, hit.chunk.path, hit.chunk.start_line, hit.chunk.chunk_id),
    )[:limit]

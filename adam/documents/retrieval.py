"""关键词检索基线；使用 BM25 排序，避免长文档只因重复词多而占优。"""

import math
from collections import Counter
from collections.abc import Sequence

from adam.documents.models import Chunk, DocumentError, SearchHit
from adam.documents.text import terms


def keyword_search(chunks: Sequence[Chunk], query: str, limit: int = 5) -> list[SearchHit]:
    """返回正得分片段，按得分和稳定来源排序；无匹配返回空列表。

    分词和 BM25 参数固定用于对照，不把得分当成答案存在的证明。
    """
    validate_query(query, limit)
    query_terms = set(terms(query))
    if not chunks or not query_terms:
        return []
    counters = [Counter(terms(chunk.title + "\n" + chunk.text)) for chunk in chunks]
    average = sum(map(lambda value: sum(value.values()), counters)) / len(counters) or 1
    frequency = Counter(word for counter in counters for word in counter)
    hits = []
    for chunk, counter in zip(chunks, counters, strict=True):
        score = 0.0
        length = sum(counter.values())
        for word in sorted(query_terms & counter.keys()):
            count = counter[word]
            inverse = math.log(1 + (len(chunks) - frequency[word] + 0.5) / (frequency[word] + 0.5))
            score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            hits.append(SearchHit(chunk, score))
    return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.path, hit.chunk.start_line))[:limit]


def validate_query(query: str, limit: int) -> None:
    """在检索前拒绝空问题、过长输入和无界返回数量。"""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise DocumentError("文档查询必须是 1～1000 字符的非空文本。")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise DocumentError("文档结果数量必须是 1～20 的整数。")

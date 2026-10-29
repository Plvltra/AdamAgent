"""协调索引刷新和检索，不理解模型协议或终端。"""

from dataclasses import dataclass

from adam.documents.index import DocumentIndex
from adam.documents.models import SearchHit
from adam.documents.retrieval import keyword_search, validate_query


@dataclass(frozen=True)
class SearchResult:
    """本次查询的候选及来源快照；没有候选不等于现实中没有答案。"""

    generation: str
    hits: tuple[SearchHit, ...]
    file_count: int
    method: str = "keyword"


class DocumentSearch:
    """每次先确认可用的当前语料，再执行检索。"""

    def __init__(self, index: DocumentIndex) -> None:
        self.index = index

    def search(self, query: str, limit: int) -> SearchResult:
        """查询失败向上报告；只有成功扫描后才允许返回空命中。"""
        validate_query(query, limit)
        corpus = self.index.refresh()
        hits = keyword_search(corpus.chunks, query, limit)
        return SearchResult(corpus.generation, tuple(hits), corpus.file_count)

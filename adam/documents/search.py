"""协调索引刷新和检索，不理解模型协议或终端。"""

from dataclasses import dataclass

from adam.contracts.reranking import RerankBackend
from adam.documents.embedding import EmbeddingBackend, FastEmbedBackend
from adam.documents.fusion import reciprocal_rank_fusion
from adam.documents.index import DocumentIndex
from adam.documents.models import FusionReport, SearchHit
from adam.documents.reranking import FastEmbedReranker, RerankReport, rerank_hits
from adam.documents.retrieval import keyword_search, validate_query
from adam.documents.vectors import VectorSearch


@dataclass(frozen=True)
class SearchResult:
    """本次查询的候选及来源快照；没有候选不等于现实中没有答案。"""

    generation: str
    hits: tuple[SearchHit, ...]
    file_count: int
    method: str = "keyword"
    fusion: FusionReport | None = None
    reranking: RerankReport | None = None


class DocumentSearch:
    """每次先确认可用的当前语料，再执行检索。"""

    def __init__(
        self,
        index: DocumentIndex,
        backend: EmbeddingBackend | None = None,
        *,
        reranker: RerankBackend | None = None,
    ) -> None:
        self.index = index
        config = index.config
        self.vectors = VectorSearch(
            index.path.with_suffix(".vectors.json"),
            backend or FastEmbedBackend(index.path.parent / "models", config.embedding_model),
            index.check_cancel,
        )
        self.reranker = reranker or FastEmbedReranker(
            index.path.parent / "rerankers", config.reranker_model
        )

    def search(self, query: str, limit: int) -> SearchResult:
        """同一份有效语料分别召回，再按配置排序；任一路失败不会伪装成完整混合结果。

        单路方法保留原行为。混合先取得有界候选池，再融合为最终 limit 条，
        不把一次已截成最终条数的列表当成完整候选池。
        可选精排失败保留融合次序并记录降级，取消仍传播，不把半次精排当成完成。
        """
        validate_query(query, limit)
        corpus = self.index.refresh()
        config = self.index.config
        fusion = None
        reranking = None
        if config.method == "hybrid":
            rerank_limit = max(limit, config.rerank_candidates) if config.rerank else limit
            candidates = max(rerank_limit, config.hybrid_candidates)
            keyword = keyword_search(corpus.chunks, query, candidates)
            self.index.check_cancel()
            semantic = self.vectors.search(
                corpus.chunks, query, candidates, config.semantic_min_score
            )
            hits = reciprocal_rank_fusion(keyword, semantic, rerank_limit, config.rrf_constant)
            fusion = FusionReport(config.rrf_constant, candidates, len(keyword), len(semantic))
            if config.rerank:
                hits, reranking = rerank_hits(
                    hits,
                    query,
                    self.reranker,
                    query_chars=config.rerank_query_chars,
                    passage_chars=config.rerank_passage_chars,
                    check_cancel=self.index.check_cancel,
                )
                hits = hits[:limit]
        elif config.method == "semantic":
            hits = self.vectors.search(corpus.chunks, query, limit, config.semantic_min_score)
        else:
            hits = keyword_search(corpus.chunks, query, limit)
        self.index.check_cancel()
        return SearchResult(
            corpus.generation, tuple(hits), corpus.file_count, config.method, fusion, reranking
        )

"""协调索引刷新和检索，不理解模型协议或终端。"""

from dataclasses import dataclass

from adam.contracts.reranking import RerankBackend
from adam.documents.contextualization import (
    ContextBuilder,
    ContextPrefix,
    ContextPrefixStore,
    ContextReport,
    ExtractiveContextBuilder,
)
from adam.documents.embedding import EmbeddingBackend, FastEmbedBackend
from adam.documents.fusion import reciprocal_rank_fusion
from adam.documents.index import Corpus, DocumentIndex
from adam.documents.models import DocumentNotice, FusionReport, SearchHit
from adam.documents.reranking import FastEmbedReranker, RerankReport, rerank_hits
from adam.documents.retrieval import keyword_search, validate_query
from adam.documents.snapshots import digest
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
    contextualization: ContextReport | None = None
    context_prefixes: tuple[ContextPrefix, ...] = ()
    notices: tuple[DocumentNotice, ...] = ()


class DocumentSearch:
    """每次先确认可用的当前语料，再执行检索。"""

    def __init__(
        self,
        index: DocumentIndex,
        backend: EmbeddingBackend | None = None,
        *,
        reranker: RerankBackend | None = None,
        context_builder: ContextBuilder | None = None,
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
        self.context_builder = context_builder or ExtractiveContextBuilder()

    def search(self, query: str, limit: int) -> SearchResult:
        """同一份有效语料分别召回，再按配置排序；任一路失败不会伪装成完整混合结果。

        单路方法保留原行为。混合先取得有界候选池，再融合为最终 limit 条，
        不把一次已截成最终条数的列表当成完整候选池。
        可选精排失败保留融合次序并记录降级，取消仍传播，不把半次精排当成完成。
        可选语境只进入关键词/向量索引文本，引用和精排正文保持原文。
        """
        validate_query(query, limit)
        corpus = self.index.refresh()
        config = self.index.config
        notes, contextualization = self._context(corpus)
        prefixes = {key: note.text for key, note in notes.items()} or None
        fusion = None
        reranking = None
        if config.method == "hybrid":
            rerank_limit = max(limit, config.rerank_candidates) if config.rerank else limit
            candidates = max(rerank_limit, config.hybrid_candidates)
            keyword = keyword_search(corpus.chunks, query, candidates, prefixes=prefixes)
            self.index.check_cancel()
            semantic = self.vectors.search(
                corpus.chunks, query, candidates, config.semantic_min_score, prefixes=prefixes
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
            hits = self.vectors.search(
                corpus.chunks, query, limit, config.semantic_min_score, prefixes=prefixes
            )
        else:
            hits = keyword_search(corpus.chunks, query, limit, prefixes=prefixes)
        self.index.check_cancel()
        return SearchResult(
            corpus.generation,
            tuple(hits),
            corpus.file_count,
            config.method,
            fusion,
            reranking,
            contextualization,
            tuple(notes[hit.chunk.chunk_id] for hit in hits if hit.chunk.chunk_id in notes),
            corpus.notices,
        )

    def _context(self, corpus: Corpus) -> tuple[dict[str, ContextPrefix], ContextReport | None]:
        """显式启用才生成说明；关闭时不读缓存，来源或说明故障仍停止该次检索。"""
        config = self.index.config
        if not config.contextualize:
            return {}, None
        notes = ContextPrefixStore(
            self.index.path.with_suffix(".contexts.json"),
            max_prefix_chars=config.context_prefix_chars,
            max_input_chars=config.context_input_chars,
            check_cancel=self.index.check_cancel,
        ).prepare(corpus, self.context_builder)
        report = ContextReport(
            self.context_builder.identity,
            config.context_prefix_chars,
            config.context_input_chars,
            digest([n.to_dict() for n in notes]),
            len(notes),
        )
        return {note.chunk_id: note for note in notes}, report

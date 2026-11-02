"""C08.3：每次重读授权语料，合并原文本候选与图来源，不发布知识。"""

from dataclasses import dataclass

from adam.documents.graph.evidence import extract_graph
from adam.documents.graph.query import GraphPaths, query_paths
from adam.documents.models import Chunk, DocumentError
from adam.documents.search import DocumentSearch, SearchResult


@dataclass(frozen=True)
class GraphCandidate:
    """一个原始片段只交付一次；methods 标明文本或图带来的来源。"""

    chunk: Chunk
    methods: tuple[str, ...]


@dataclass(frozen=True)
class GraphRetrieval:
    """文本检索原报告与有证据路径分开保存；不混合不可比的相关性分数。"""

    text: SearchResult
    graph: GraphPaths
    candidates: tuple[GraphCandidate, ...]


class GraphSearch:
    """借用现有 DocumentSearch 及其授权索引；不持有第二份图缓存或长期知识。"""

    def __init__(self, text_search: DocumentSearch) -> None:
        self.text_search = text_search

    def search(
        self,
        query: str,
        source: str,
        target: str | None = None,
        *,
        limit: int = 10,
        max_hops: int = 2,
    ) -> GraphRetrieval:
        """在同一来源代次合并候选；源变化或权限失败时停止，不使用上次图。

        limit 是原文本召回数；图另受 100 节点/1000 边及 1000 提取边的固定上限。
        只返回实际原文关系；知识发布仍必须走既有 B12 显式人工确认入口。
        """
        text = self.text_search.search(query, limit)
        corpus = self.text_search.index.refresh()
        if corpus.generation != text.generation:
            raise DocumentError("图与文本查询之间来源已变化，请显式重新查询。")
        paths = query_paths(extract_graph(corpus), source, target, max_hops=max_hops)
        chunks = {c.chunk_id: c for c in corpus.chunks}
        candidates: dict[str, GraphCandidate] = {
            hit.chunk.chunk_id: GraphCandidate(hit.chunk, ("text",)) for hit in text.hits
        }
        for path in paths.paths:
            for edge in path:
                key = edge.evidence.chunk_id
                prior = candidates.get(key)
                methods = ("text", "graph") if prior and "text" in prior.methods else ("graph",)
                candidates[key] = GraphCandidate(chunks[key], methods)
        self.text_search.index.check_cancel()
        return GraphRetrieval(text, paths, tuple(candidates.values()))

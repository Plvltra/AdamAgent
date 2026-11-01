"""对当前模式允许的可信工具目录做有界本地排序，不选择 Schema 或执行工具。"""

from dataclasses import dataclass
from threading import RLock

from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.embedding import EmbeddingBackend, unit_vector
from adam.contracts.errors import ToolError
from adam.contracts.execution import ExecutionMode
from adam.contracts.tool_catalog import ToolListing
from adam.tool_calls.discovery import ToolDirectory


@dataclass(frozen=True)
class ToolRanking:
    """候选与实际排序来源；分数只表示余弦相似度，不是正确率、执行证据或权限。"""

    entries: tuple[ToolListing, ...]
    directory_version: str
    strategy: str
    scores: tuple[tuple[str, float], ...] = ()
    embedding: str | None = None
    fallback_reason: str | None = None


class SemanticToolRanker:
    """只在内存缓存最新一次允许范围的向量；目录、模式、分组或模型变化即重建。

    backend 由宿主注入，构造不加载模型、不下载、不读写工具或配置。
    调用方仍须沿原发现与实际请求名单检查，再由执行器处理路径和批准。
    """

    def __init__(
        self, config: ToolDiscoveryConfig, backend: EmbeddingBackend | None = None
    ) -> None:
        self.config, self.backend = config, backend
        self._key: tuple[str, ExecutionMode, str | None, str] | None = None
        self._vectors: tuple[list[float], ...] = ()
        self._lock = RLock()

    def rank(
        self, directory: ToolDirectory, mode: ExecutionMode, query: str, *, group: str | None = None
    ) -> ToolRanking:
        """返回允许目录中的完整候选排序；查询最多200字符，空查询按目录顺序返回。

        先按能力和分组过滤再编码；模型失败、坏向量或目录超限时整批降级为原有子串匹配，
        固定原因不含后端异常正文。Ctrl+C 继续传播；无结果与后端不可用明确区分。
        """
        if not isinstance(query, str) or len(query) > 200:
            raise ToolError("工具目录查询必须为最多200字符的字符串。")
        allowed = directory.listings(mode)
        if group is not None and group not in {entry.group for entry in allowed}:
            raise ToolError("未知或当前模式不可用的工具分组。")
        entries = tuple(entry for entry in allowed if group is None or entry.group == group)
        if self.config.ranking == "keyword" or not query.strip() or not entries:
            return ToolRanking(_keyword(entries, query), directory.version, "keyword_v1")
        reason = "candidate_limit" if len(entries) > self.config.max_index_tools else None
        if reason is None:
            with self._lock:
                try:
                    return self._rank(directory.version, mode, group, entries, query)
                except Exception:
                    # 外部编码器故障不能伪装成没有相关工具；不记录潜在敏感异常文本。
                    self._key, self._vectors = None, ()
                    reason = "embedding_unavailable"
        return ToolRanking(
            _keyword(entries, query),
            directory.version,
            "keyword_fallback_v1",
            fallback_reason=reason,
        )

    def _rank(
        self,
        version: str,
        mode: ExecutionMode,
        group: str | None,
        entries: tuple[ToolListing, ...],
        query: str,
    ) -> ToolRanking:
        """整批向量与查询校验后才替换缓存；只编码名称、分组和摘要，不给编码器完整 Schema。"""
        if self.backend is None:
            raise ValueError("no embedding backend")
        identity = self.backend.identity
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 512:
            raise ValueError("invalid embedding identity")
        key = (version, mode, group, identity)
        vectors = self._vectors if key == self._key else ()
        if not vectors:
            raw = self.backend.passages([_text(entry) for entry in entries])
            if not isinstance(raw, list) or len(raw) != len(entries):
                raise ValueError("incomplete tool vectors")
            vectors = tuple(unit_vector(vector) for vector in raw)
        encoded_query = unit_vector(self.backend.query(query))
        if any(len(vector) != len(encoded_query) for vector in vectors):
            raise ValueError("incompatible tool vectors")
        if self.backend.identity != identity:
            raise ValueError("embedding identity changed during request")
        self._key, self._vectors = key, vectors
        ranked = [
            (
                entry,
                max(-1.0, min(1.0, sum(a * b for a, b in zip(vector, encoded_query, strict=True)))),
            )
            for entry, vector in zip(entries, vectors, strict=True)
        ]
        # 准确名称始终可找；同分保持注册顺序，不把相关性分数变成授权。
        ranked = [
            (entry, score)
            for entry, score in ranked
            if score >= self.config.semantic_min_score or entry.name.casefold() == query.casefold()
        ]
        ranked.sort(
            key=lambda item: (item[0].name.casefold() == query.casefold(), item[1]), reverse=True
        )
        return ToolRanking(
            tuple(entry for entry, _ in ranked),
            version,
            "semantic_v1",
            tuple((entry.name, score) for entry, score in ranked),
            identity,
        )


def _text(entry: ToolListing) -> str:
    return entry.name + " " + entry.group + " " + entry.summary


def _keyword(entries: tuple[ToolListing, ...], query: str) -> tuple[ToolListing, ...]:
    """与原目录查询使用同一子串规则；降级不引入模型猜测的名称或丢弃候选后冒充全量。"""
    return tuple(entry for entry in entries if query.casefold() in _text(entry).casefold())

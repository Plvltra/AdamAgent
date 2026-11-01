"""按标题组织二层知识目录；概览是有来源的摘录，原始片段保持独立。"""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.documents.index import Corpus
from adam.documents.models import Chunk, DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot


@dataclass(frozen=True)
class CatalogLimits:
    """限制主题、全部引用和每主题概览大小；超过总覆盖上限时拒绝，不静默漏掉资料。"""

    max_topics: int = 200
    max_references: int = 10000
    max_overview_chars: int = 600
    max_overview_sources: int = 3

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_topics, 1000),
            (self.max_references, 50000),
            (self.max_overview_chars, 4000),
            (self.max_overview_sources, 8),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise DocumentError("知识目录限制必须是有效的有界整数。")


@dataclass(frozen=True)
class CatalogReference:
    """目录第二层的原文引用；chunk_id 与 version 绑定位置和完整原文，不能自行扩权。"""

    chunk_id: str
    path: str
    version: str
    start_line: int
    end_line: int
    text_chars: int


@dataclass(frozen=True)
class OverviewExcerpt:
    """概览中的逐字原文前缀；start/end 是分块内字符区间，不是整个文件偏移。"""

    chunk_id: str
    start: int
    end: int
    text: str
    truncated: bool


@dataclass(frozen=True)
class KnowledgeTopic:
    """同名标题合并为主题；overview 只是代表摘录，references 列出该主题全部原文。"""

    topic_id: str
    title: str
    references: tuple[CatalogReference, ...]
    overview: tuple[OverviewExcerpt, ...]


@dataclass(frozen=True)
class KnowledgeCatalog:
    """完整二层目录，绑定原始 Corpus 和概览规则；非抽象总结，不推断跨文档结论。"""

    source_generation: str
    catalog_version: str
    topics: tuple[KnowledgeTopic, ...]
    limits: CatalogLimits

    def to_dict(self) -> dict[str, Any]:
        """保存全部引用及受限概览；读取入口另行限制输出，不能把摘录覆盖当成全库阅读。"""
        return {
            "format": "knowledge_catalog_v1",
            "method": "extractive-headings-v1",
            **asdict(self),
        }


def build_catalog(
    corpus: Corpus,
    limits: CatalogLimits | None = None,
    *,
    check_cancel: Callable[[], None] = lambda: None,
) -> KnowledgeCatalog:
    """从已授权快照生成主题概览和全部片段引用，不重新读取文件或请求模型。

    标题按空白归一及大小写合并；无标题按文件名分组。概览优先不同文件的非标题段，
    明确保留截断与引用范围；资料过多、重复身份或同文件混合版本会拒绝整份目录。
    """
    limits = limits or CatalogLimits()
    if len(corpus.chunks) > limits.max_references:
        raise DocumentError("知识目录的引用总量超过上限，请缩小资料范围。")
    groups: dict[str, tuple[str, list[Chunk]]] = {}
    identities = set()
    versions: dict[str, str] = {}
    for chunk in corpus.chunks:
        check_cancel()
        _validate_reference(_reference(chunk))
        if chunk.chunk_id in identities or versions.get(chunk.path, chunk.version) != chunk.version:
            raise DocumentError("知识目录输入包含重复片段或同文件混合版本。")
        identities.add(chunk.chunk_id)
        versions[chunk.path] = chunk.version
        title = " ".join((chunk.title or Path(chunk.path).stem).split())[:200]
        if not title:
            raise DocumentError("知识目录主题名为空，请为文档提供标题或有效文件名。")
        key = title.casefold()
        groups.setdefault(key, (title, []))[1].append(chunk)
    if len(groups) > limits.max_topics:
        raise DocumentError("知识目录主题数超过上限，未返回不完整的目录。")
    topics = []
    for key, (title, chunks) in sorted(groups.items()):
        check_cancel()
        ordered = sorted(chunks, key=lambda c: (c.path, c.start_line))
        topics.append(
            KnowledgeTopic(
                digest(["topic", key])[:16],
                title,
                tuple(_reference(chunk) for chunk in ordered),
                _overview(ordered, limits),
            )
        )
    payload = {
        "source_generation": corpus.generation,
        "topics": [asdict(t) for t in topics],
        "limits": asdict(limits),
    }
    check_cancel()
    return KnowledgeCatalog(corpus.generation, digest(payload), tuple(topics), limits)


class KnowledgeCatalogStore:
    """缓存可重建目录，复用文件锁和原子快照；源或规则变化后完整重建，不修改源文档。"""

    def __init__(
        self,
        path: Path,
        *,
        limits: CatalogLimits | None = None,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> None:
        self.path, self.limits = path, limits or CatalogLimits()
        self.check_cancel = check_cancel
        self.lock = FileLock(str(path.with_suffix(".lock")), timeout=5)

    def prepare(self, corpus: Corpus) -> KnowledgeCatalog:
        """校验旧缓存后返回当前二层目录；损坏、取消或保存失败不返回旧概览冒充新资料。"""
        self.check_cancel()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                raw = load_snapshot(self.path)
                previous = _decode(raw) if raw is not None else None
                current = build_catalog(corpus, self.limits, check_cancel=self.check_cancel)
                self.check_cancel()
                if current != previous:
                    save_snapshot(self.path, current.to_dict())
                return current
        except Timeout as error:
            raise DocumentError("知识目录正在刷新，请稍后重试。") from error
        except OSError as error:
            raise DocumentError("知识目录读写失败；旧文件保留，本次没有返回目录。") from error


def _overview(chunks: list[Chunk], limits: CatalogLimits) -> tuple[OverviewExcerpt, ...]:
    """先给不同文件一个代表片段，再补其他片段；输出是有标记的逐字前缀而非生成结论。"""
    candidates = [c for c in chunks if re.fullmatch(r"#{1,6}\s+[^\n]+", c.text) is None] or chunks
    first: dict[str, Chunk] = {}
    for chunk in candidates:
        first.setdefault(chunk.path, chunk)
    chosen = list(first.values())
    chosen_ids = {c.chunk_id for c in chosen}
    chosen.extend(c for c in candidates if c.chunk_id not in chosen_ids)
    chosen = chosen[: limits.max_overview_sources]
    remaining = limits.max_overview_chars
    excerpts = []
    for i, chunk in enumerate(chosen):
        size = min(len(chunk.text), max(1, remaining // (len(chosen) - i)))
        if not remaining:
            break
        excerpts.append(
            OverviewExcerpt(chunk.chunk_id, 0, size, chunk.text[:size], size < len(chunk.text))
        )
        remaining -= size
    return tuple(excerpts)


def _reference(chunk: Chunk) -> CatalogReference:
    return CatalogReference(
        chunk.chunk_id, chunk.path, chunk.version, chunk.start_line, chunk.end_line, len(chunk.text)
    )


def _validate_reference(reference: CatalogReference) -> None:
    """引用只接受明确版本与正行号；路径是来源身份，访问时仍须使用当前授权索引。"""
    if (
        not isinstance(reference.chunk_id, str)
        or re.fullmatch(r"[0-9a-f]{16}", reference.chunk_id) is None
        or not isinstance(reference.path, str)
        or not reference.path
        or not isinstance(reference.version, str)
        or re.fullmatch(r"[0-9a-f]{64}", reference.version) is None
        or type(reference.start_line) is not int
        or type(reference.end_line) is not int
        or not 1 <= reference.start_line <= reference.end_line
        or type(reference.text_chars) is not int
        or reference.text_chars < 1
    ):
        raise DocumentError("知识目录的原文引用格式无效。")


def _decode(raw: dict[str, Any]) -> KnowledgeCatalog:
    """校验二层关系、来源版本和概览区间；语义过时由当前 Corpus 重建，结构损坏则报错。"""
    try:
        if (
            raw.keys()
            != {"format", "method", "source_generation", "catalog_version", "topics", "limits"}
            or raw["format"] != "knowledge_catalog_v1"
            or raw["method"] != "extractive-headings-v1"
        ):
            raise ValueError("catalog schema")
        if not isinstance(raw["topics"], list) or not isinstance(raw["limits"], dict):
            raise ValueError("catalog containers")
        limits = CatalogLimits(**raw["limits"])
        if len(raw["topics"]) > limits.max_topics:
            raise ValueError("topic limit")
        topics, ids, reference_ids = [], set(), set()
        for value in raw["topics"]:
            topic = KnowledgeTopic(
                **{
                    **value,
                    "references": tuple(CatalogReference(**r) for r in value["references"]),
                    "overview": tuple(OverviewExcerpt(**e) for e in value["overview"]),
                }
            )
            if (
                not isinstance(topic.title, str)
                or not topic.title
                or len(topic.title) > 200
                or topic.topic_id != digest(["topic", topic.title.casefold()])[:16]
                or topic.topic_id in ids
                or not topic.references
            ):
                raise ValueError("topic identity")
            ids.add(topic.topic_id)
            references = {}
            for reference in topic.references:
                _validate_reference(reference)
                if reference.chunk_id in reference_ids:
                    raise ValueError("duplicate reference")
                reference_ids.add(reference.chunk_id)
                references[reference.chunk_id] = reference
            if len(topic.overview) > limits.max_overview_sources or not topic.overview:
                raise ValueError("overview count")
            seen = set()
            for excerpt in topic.overview:
                source = references[excerpt.chunk_id]
                if (
                    excerpt.chunk_id in seen
                    or type(excerpt.start) is not int
                    or excerpt.start != 0
                    or type(excerpt.end) is not int
                    or not 1 <= excerpt.end <= source.text_chars
                    or not isinstance(excerpt.text, str)
                    or len(excerpt.text) != excerpt.end
                    or type(excerpt.truncated) is not bool
                    or excerpt.truncated != (excerpt.end < source.text_chars)
                ):
                    raise ValueError("overview range")
                seen.add(excerpt.chunk_id)
            if sum(len(e.text) for e in topic.overview) > limits.max_overview_chars:
                raise ValueError("overview budget")
            topics.append(topic)
        if len(reference_ids) > limits.max_references:
            raise ValueError("reference limit")
        payload = {
            "source_generation": raw["source_generation"],
            "topics": [asdict(t) for t in topics],
            "limits": asdict(limits),
        }
        if not isinstance(raw["source_generation"], str) or raw["catalog_version"] != digest(
            payload
        ):
            raise ValueError("catalog version")
        return KnowledgeCatalog(
            raw["source_generation"], raw["catalog_version"], tuple(topics), limits
        )
    except (ValueError, TypeError, KeyError, AttributeError, DocumentError) as error:
        raise DocumentError("知识目录缓存损坏或版本不支持；保留文件，请移走后重建。") from error

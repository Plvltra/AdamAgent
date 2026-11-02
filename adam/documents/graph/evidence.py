"""C08.1：从已授权文档片段的显式实体标记提取有来源的关系候选。"""

import re
from dataclasses import dataclass

from adam.documents.index import Corpus
from adam.documents.models import DocumentError
from adam.documents.snapshots import digest

_ENTITY = (
    r"\[(?P<{p}id>[a-z][a-z0-9_]*:[A-Za-z0-9_-]{{1,64}})\|(?P<{p}label>[^\[\]|\r\n]{{1,100}})\]"
)
_RELATION = re.compile(
    _ENTITY.format(p="s") + r"\s+(?P<relation>calls|maintained_by)\s+" + _ENTITY.format(p="o")
)


@dataclass(frozen=True)
class Entity:
    """显式类型化 ID 决定身份；同名但不同 ID 的对象不自动合并。"""

    identity: str
    label: str


@dataclass(frozen=True)
class EdgeEvidence:
    """引用原 Chunk 和文件版本；字符范围在该片段内，行号保留原片段范围。"""

    chunk_id: str
    path: str
    version: str
    start_line: int
    end_line: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Edge:
    """一项原文关系候选；方向固定，未经过人工发布或模型事实审查。"""

    identity: str
    subject: Entity
    relation: str
    target: Entity
    evidence: EdgeEvidence
    extraction: str = "explicit_marker_v1"


@dataclass(frozen=True)
class EvidenceGraph:
    """只对应一次成功语料快照；不保存跨版本实体归并或长期知识。"""

    generation: str
    edges: tuple[Edge, ...]


def extract_graph(corpus: Corpus, *, max_edges: int = 1000) -> EvidenceGraph:
    """提取 `[type:id|名称] calls/maintained_by [type:id|名称]` 关系候选。

    只处理调用方已授权并刷新成功的 Corpus，不自行读取路径。没有显式标记的普通
    文字不推测成关系；来源冲突全部保留。超限整批失败，不返回伪完整图或发布知识。
    """
    if type(max_edges) is not int or not 1 <= max_edges <= 10000:
        raise ValueError("max_edges must be 1..10000")
    edges: dict[str, Edge] = {}
    for chunk in corpus.chunks:
        for match in _RELATION.finditer(chunk.text):
            evidence = EdgeEvidence(
                chunk.chunk_id,
                chunk.path,
                chunk.version,
                chunk.start_line,
                chunk.end_line,
                match.start(),
                match.end(),
                match.group(),
            )
            identity = digest([chunk.chunk_id, chunk.version, match.start(), match.end()])
            edges[identity] = Edge(
                identity,
                Entity(match["sid"], match["slabel"]),
                match["relation"],
                Entity(match["oid"], match["olabel"]),
                evidence,
            )
            if len(edges) > max_edges:
                raise DocumentError("关系候选超出上限，未返回部分图。")
    return EvidenceGraph(corpus.generation, tuple(edges.values()))

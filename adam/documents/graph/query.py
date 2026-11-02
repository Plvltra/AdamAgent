"""C08.2：显式实体 ID 的有界有向路径；只返回原边，不合成推断事实。"""

import re
from collections import defaultdict, deque
from dataclasses import dataclass

from adam.documents.graph.evidence import Edge, EvidenceGraph
from adam.documents.models import DocumentError


@dataclass(frozen=True)
class GraphPaths:
    """每条路径保留各边独立证据；空结果只表示当前固定图及跳数内没有依据。"""

    generation: str
    paths: tuple[tuple[Edge, ...], ...]
    visited_nodes: int
    traversed_edges: int


def query_paths(
    graph: EvidenceGraph,
    source: str,
    target: str | None = None,
    *,
    max_hops: int = 2,
    max_nodes: int = 100,
    max_edges: int = 1000,
) -> GraphPaths:
    """查询固定快照中无环的有向路径；必须用 ID，不按同名标签自动猜测身份。

    target 为空时返回跳数内的全部路径，可从末边读取维护者等关系。节点/边计数
    包括本次遍历，超过上限整次报错而非伪装无答案；不会读取文件或发布知识。
    """
    identity = r"[a-z][a-z0-9_]*:[A-Za-z0-9_-]{1,64}"
    if (
        not isinstance(source, str)
        or re.fullmatch(identity, source) is None
        or target is not None
        and (not isinstance(target, str) or re.fullmatch(identity, target) is None)
    ):
        raise ValueError("Graph queries require explicit typed entity IDs")
    for value, maximum in ((max_hops, 4), (max_nodes, 1000), (max_edges, 10000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Invalid graph traversal limit")
    outgoing: dict[str, list[Edge]] = defaultdict(list)
    known: set[str] = set()
    for edge in graph.edges:
        outgoing[edge.subject.identity].append(edge)
        known.update((edge.subject.identity, edge.target.identity))
    if source not in known or target is not None and target not in known:
        return GraphPaths(graph.generation, (), 0, 0)
    queue: deque[tuple[str, tuple[Edge, ...], frozenset[str]]] = deque(
        [(source, (), frozenset({source}))]
    )
    visited = {source}
    traversed = 0
    found = []
    while queue:
        node, path, in_path = queue.popleft()
        if len(path) == max_hops:
            continue
        for edge in outgoing[node]:
            traversed += 1
            if traversed > max_edges:
                raise DocumentError("图查询达到边上限，未返回不完整答案。")
            next_node = edge.target.identity
            if next_node in in_path:
                continue
            visited.add(next_node)
            if len(visited) > max_nodes:
                raise DocumentError("图查询达到节点上限，未返回不完整答案。")
            next_path = (*path, edge)
            if target is None or next_node == target:
                found.append(next_path)
            if next_node != target:
                queue.append((next_node, next_path, in_path | {next_node}))
    return GraphPaths(graph.generation, tuple(found), len(visited), traversed)

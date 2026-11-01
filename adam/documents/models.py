"""文档片段及命中结果；保留内容版本和可核查的位置。"""

from dataclasses import asdict, dataclass

from adam.contracts.errors import ToolError


class DocumentError(ToolError):
    """文档检索的受控失败；不能将读取失败伪装成没有匹配。"""


@dataclass(frozen=True)
class Chunk:
    """源文档的一个片段，行号从 1 开始，version 是原文件字节的 SHA-256。"""

    chunk_id: str
    path: str
    title: str
    start_line: int
    end_line: int
    text: str
    version: str

    def to_dict(self) -> dict[str, str | int]:
        """返回可以写入索引和工具结果的来源快照。"""
        return asdict(self)


@dataclass(frozen=True)
class RetrievalRank:
    """片段在一个召回列表的排名和原得分；不同检索方法的分数不能直接相加。"""

    method: str
    rank: int
    score: float


@dataclass(frozen=True)
class FusionReport:
    """本次融合使用的常量和各路候选数；候选上限不是答案正确性的阈值。"""

    rrf_constant: int
    candidate_limit: int
    keyword_candidates: int
    semantic_candidates: int


@dataclass(frozen=True)
class SearchHit:
    """相关性得分只用于排序，不代表答案正确率或事实置信度。"""

    chunk: Chunk
    score: float
    retrieval_ranks: tuple[RetrievalRank, ...] = ()

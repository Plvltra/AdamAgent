"""文档片段及命中结果；保留内容版本和可核查的位置。"""

from dataclasses import asdict, dataclass

from adam.contracts.errors import ToolError


class DocumentError(ToolError):
    """文档检索的受控失败；不能将读取失败伪装成没有匹配。"""


@dataclass(frozen=True)
class Chunk:
    """行号从1开始，version 为原文件 SHA-256；PDF 的 page 是物理页，行号属页内提取文本。"""

    chunk_id: str
    path: str
    title: str
    start_line: int
    end_line: int
    text: str
    version: str
    page: int | None = None

    def to_dict(self) -> dict[str, str | int]:
        """返回可以写入索引和工具结果的来源快照。"""
        result = asdict(self)
        if self.page is None:
            result.pop("page")
        return result


@dataclass(frozen=True)
class DocumentNotice:
    """未覆盖的 PDF 内容；没有提取到文本不能据此推断页面为空或没有答案。"""

    path: str
    version: str
    page: int
    kind: str


def coverage_metadata(notices: tuple[DocumentNotice, ...]) -> dict[str, object]:
    """保留总数和最多五条完整来源，避免扫描件的每页提示挤占全部工具输出。"""
    if not notices:
        return {}
    return {
        "source_coverage": {
            "status": "incomplete_pdf_perception",
            "notice_count": len(notices),
            "notices": [asdict(notice) for notice in notices[:5]],
            "notices_truncated": len(notices) > 5,
            "hint": "PDF 图像未识别；无文本页不代表空白。用 read_pdf 按物理页核查。",
        }
    }


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
    rerank_score: float | None = None

"""搜索候选的有界契约；摘要不是已抓取网页的正文或引用证据。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from adam.contracts.cancellation import CancellationToken


@dataclass(frozen=True)
class SearchLimits:
    """单次搜索的候选、文本、响应字节和完整操作期限上限。"""

    max_results: int = 5
    max_query_chars: int = 512
    max_title_chars: int = 512
    max_snippet_chars: int = 768
    max_bytes: int = 1048576
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_results", 20),
            ("max_query_chars", 1024),
            ("max_title_chars", 2048),
            ("max_snippet_chars", 4000),
            ("max_bytes", 10000000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} 超出允许范围。")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds 必须大于 0 且不超过 120。")


@dataclass(frozen=True)
class SearchCandidate:
    """用于选择下一次读取的链接；标题和摘要均为未信任的远端资料。"""

    title: str
    url: str
    snippet: str
    title_truncated: bool = False
    snippet_truncated: bool = False


@dataclass(frozen=True)
class SearchResult:
    """保留搜索响应版本和实际范围；响应摘要不代表候选页面的内容版本。"""

    provider: str
    scope: str
    query: str
    searched_at: str
    endpoint: str
    response_sha256: str
    response_byte_size: int
    candidates: tuple[SearchCandidate, ...]
    has_more: bool
    evidence_kind: Literal["search_snippets"] = "search_snippets"
    schema_version: int = 1


class SearchBackend(Protocol):
    """只返回有界候选的独立搜索端口；资源由创建方关闭。"""

    def search(self, query: str, *, cancel: CancellationToken | None = None) -> SearchResult:
        """查询候选；无结果是空元组，失败抛 WebError，取消传播原中断。"""
        ...

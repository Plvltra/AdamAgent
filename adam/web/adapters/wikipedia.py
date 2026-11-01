"""MediaWiki Action API 搜索适配器；首版范围为 English Wikipedia 百科。"""

import hashlib
import json
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit

from adam.contracts.cancellation import CancellationToken
from adam.web.models import PageResponse, PageTransport, WebError
from adam.web.policy import NetworkPolicy
from adam.web.search_models import SearchCandidate, SearchLimits, SearchResult

WIKIPEDIA_ENDPOINT = "https://en.wikipedia.org/w/api.php"


class WikipediaSearch:
    """借用可取消 HTTP 端口；不读取会话或模型配置，也不自动重试。"""

    def __init__(
        self,
        transport: PageTransport,
        policy: NetworkPolicy,
        limits: SearchLimits,
        *,
        endpoint: str = WIKIPEDIA_ENDPOINT,
    ) -> None:
        """固定百科端点；替代端点仅允许宿主明确注入的本地验收服务。"""
        self._transport = transport
        self._policy = policy
        self._limits = limits
        parts = urlsplit(policy.normalize(endpoint))
        if parts.query or urlsplit(endpoint).fragment:
            raise ValueError("搜索端点不能包含查询或片段。")
        self._endpoint = urlunsplit(parts)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        if self._endpoint != WIKIPEDIA_ENDPOINT and self._origin not in policy.local_test_origins:
            raise ValueError("仅支持 Wikipedia 端点或明确授权的本地验收端点。")

    def search(self, query: str, *, cancel: CancellationToken | None = None) -> SearchResult:
        """返回百科候选和搜索响应摘要；不跟随 API 重定向，不读取候选页面。"""
        token = cancel or CancellationToken()
        token.check()
        deadline = time.monotonic() + self._limits.timeout_seconds
        query = self._query(query)
        url = self._url(query)
        response = self._transport.read(
            url, remaining_seconds=self._remaining(deadline), cancel=token
        )
        token.check()
        data = self._response_data(response)
        candidates = self._candidates(data, token)
        has_more = _has_more(data)
        self._remaining(deadline)
        token.check()
        return SearchResult(
            provider="wikipedia",
            scope="English Wikipedia encyclopedia; not general web search",
            query=query,
            searched_at=datetime.now(UTC).isoformat(),
            endpoint=self._endpoint,
            response_sha256=hashlib.sha256(response.body).hexdigest(),
            response_byte_size=len(response.body),
            candidates=candidates,
            has_more=has_more,
        )

    def _query(self, query: str) -> str:
        """拒绝空白、超长、控制字符和孤立代理字符，不把输入回显到错误。"""
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > self._limits.max_query_chars
            or any(
                ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in query
            )
        ):
            raise WebError("invalid_query", "搜索关键词为空、无效或超过字符上限。")
        return " ".join(query.split())

    def _url(self, query: str) -> str:
        params = urlencode(
            {
                "action": "query",
                "list": "search",
                "format": "json",
                "formatversion": 2,
                "utf8": 1,
                "srnamespace": 0,
                "srprop": "snippet",
                "srinfo": "",
                "srlimit": self._limits.max_results,
                "srsearch": query,
                "maxlag": 5,
            }
        )
        url = f"{self._endpoint}?{params}"
        if len(url) > 4096:
            raise WebError("invalid_query", "搜索关键词编码后超过 URL 上限。")
        return self._policy.normalize(url)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebError("timeout", "搜索超时。")
        return remaining

    def _response_data(self, response: PageResponse) -> dict[str, object]:
        """先区分 HTTP/API 故障，再读取严格 JSON；远端错误正文不进入提示。"""
        if response.status == 429:
            raise WebError("rate_limited", "搜索服务限流；未自动重试。")
        if 300 <= response.status < 400:
            raise WebError("unexpected_redirect", "搜索 API 意外重定向；未跟随目标。")
        if response.status != 200:
            raise WebError(f"http_{response.status}", f"搜索服务返回 HTTP {response.status}。")
        if len(response.body) > self._limits.max_bytes:
            raise WebError("too_large", "搜索响应超过字节上限。")
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise WebError("unsupported_type", "搜索服务未返回 JSON。")
        data = _decode(response.body)
        if "error" in data:
            error = data["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code in ("ratelimited", "maxlag"):
                raise WebError("rate_limited", "搜索服务限流或繁忙；未自动重试。")
            raise WebError("api_error", "搜索 API 返回错误；未采用远端错误正文。")
        if "warnings" in data:
            raise WebError("api_warning", "搜索 API 返回警告，未把可能不完整的结果当成成功。")
        return data

    def _candidates(
        self, data: dict[str, object], cancel: CancellationToken
    ) -> tuple[SearchCandidate, ...]:
        """全批校验后交付候选；错类型、重复或超量条目使整批失败。"""
        result = data.get("query")
        entries = result.get("search") if isinstance(result, dict) else None
        if not isinstance(entries, list) or len(entries) > self._limits.max_results:
            raise _invalid()
        candidates: list[SearchCandidate] = []
        seen: set[int] = set()
        for entry in entries:
            cancel.check()
            candidate, page_id = self._candidate(entry)
            if page_id in seen:
                raise _invalid()
            seen.add(page_id)
            candidates.append(candidate)
        return tuple(candidates)

    def _candidate(self, entry: object) -> tuple[SearchCandidate, int]:
        """按数值页 ID 构造同源链接，摘要仅提取文本并显式标记截断。"""
        if not isinstance(entry, dict):
            raise _invalid()
        page_id, title, snippet = entry.get("pageid"), entry.get("title"), entry.get("snippet")
        namespace = entry.get("ns")
        if (
            type(page_id) is not int
            or not 1 <= page_id <= 2**63 - 1
            or type(namespace) is not int
            or namespace != 0
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(snippet, str)
        ):
            raise _invalid()
        title = _plain(title)
        parser = _Snippet()
        parser.feed(snippet)
        parser.close()
        snippet = _plain("".join(parser.text))
        # 不采用远端条目中的任意 URL；候选链接不随摘要中的指令改变。
        return SearchCandidate(
            title=title[: self._limits.max_title_chars],
            url=f"{self._origin}/w/index.php?curid={page_id}",
            snippet=snippet[: self._limits.max_snippet_chars],
            title_truncated=len(title) > self._limits.max_title_chars,
            snippet_truncated=len(snippet) > self._limits.max_snippet_chars,
        ), page_id


def _invalid() -> WebError:
    return WebError("invalid_response", "搜索响应不符合约定，未返回部分候选。")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _decode(body: bytes) -> dict[str, object]:
    """严格读取 UTF-8 JSON；拒绝重复字段、非有限数值和过深结构。"""
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(data, dict):
        raise _invalid()
    return data


def _has_more(data: dict[str, object]) -> bool:
    if "continue" not in data:
        return False
    continuation = data["continue"]
    if not isinstance(continuation, dict):
        raise _invalid()
    offset = continuation.get("sroffset")
    if type(offset) is not int or offset < 0 or not isinstance(continuation.get("continue"), str):
        raise _invalid()
    return True


def _plain(text: str) -> str:
    text = " ".join(text.split())
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise _invalid()
    return text


class _Snippet(HTMLParser):
    """只保留摘要文本，不执行 HTML 或保留属性；脚本等非正文直接排除。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self._hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._hidden.append(tag)
        if not self._hidden and tag in {"br", "p", "div", "li"}:
            self.text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden and tag == self._hidden[-1]:
            self._hidden.pop()
        if not self._hidden and tag in {"p", "div", "li"}:
            self.text.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.text.append(data)

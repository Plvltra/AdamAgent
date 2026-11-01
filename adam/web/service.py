"""网页抓取协调、正文提取与不可变来源快照。"""

import hashlib
import time
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urljoin

from adam.contracts.cancellation import CancellationToken
from adam.web.models import FetchLimits, PageTransport, WebError, WebPage
from adam.web.policy import NetworkPolicy


class WebReader:
    """借用网络端口；不持有会话、配置全集或供应商客户端。"""

    def __init__(
        self, transport: PageTransport, policy: NetworkPolicy, limits: FetchLimits
    ) -> None:
        self._transport = transport
        self._policy = policy
        self._limits = limits

    def fetch(self, url: str, *, cancel: CancellationToken | None = None) -> WebPage:
        """读取有界正文及来源版本；失败不返回伪有效快照，不重试请求。"""
        token = cancel or CancellationToken()
        token.check()
        requested = current = self._policy.normalize(url)
        redirects: list[str] = []
        deadline = time.monotonic() + self._limits.timeout_seconds
        while True:
            token.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebError("timeout", "网页抓取超时。")
            response = self._transport.read(current, remaining_seconds=remaining, cancel=token)
            if response.status in {301, 302, 303, 307, 308}:
                if not response.location:
                    raise WebError("invalid_redirect", "网页重定向缺少目标。")
                if len(redirects) >= self._limits.max_redirects:
                    raise WebError("redirect_limit", "网页重定向次数超过上限。")
                current = self._policy.normalize(urljoin(current, response.location))
                if current in (requested, *redirects):
                    raise WebError("redirect_loop", "网页重定向形成循环。")
                redirects.append(current)
                continue
            if response.status != 200:
                raise WebError(f"http_{response.status}", f"网页返回 HTTP {response.status}。")
            token.check()
            return self._snapshot(
                requested, current, redirects, response.body, response.content_type
            )

    def _snapshot(
        self, requested: str, final: str, redirects: list[str], body: bytes, content_type: str
    ) -> WebPage:
        """只保留可见正文；摘要指向原实体，脚本和样式不是正文。"""
        if len(body) > self._limits.max_bytes:
            raise WebError("too_large", "网页实体超过字节上限。")
        header = Message()
        header["content-type"] = content_type
        if header.get_content_type() not in {"text/html", "text/plain", "application/xhtml+xml"}:
            raise WebError("unsupported_type", "网页不是支持的 HTML 或纯文本。")
        try:
            decoded = body.decode(header.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            raise WebError("unsupported_charset", "网页声明了不支持的字符编码。") from None
        title = ""
        text = decoded
        if header.get_content_type() != "text/plain":
            parser = _ReadableHTML()
            parser.feed(decoded)
            title = " ".join(" ".join(parser.title).split())[:512]
            text = " ".join(" ".join(parser.text).split())
        return WebPage(
            requested_url=requested,
            final_url=final,
            fetched_at=datetime.now(UTC).isoformat(),
            sha256=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            title=title,
            text=text[: self._limits.max_chars],
            content_type=content_type,
            truncated=len(text) > self._limits.max_chars,
            redirects=tuple(redirects),
        )


class _ReadableHTML(HTMLParser):
    """保守排除非正文元素；不执行 JavaScript，也不宣称完整浏览器可见性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.title: list[str] = []
        self._hidden: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._hidden.append(tag)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if self._hidden and tag == self._hidden[-1]:
            self._hidden.pop()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._in_title:
            self.title.append(data)
        else:
            self.text.append(data)

"""由可信宿主固定浏览器可访问站点，不从页面文字扩大权限。"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from adam.browser.models import BrowserError


def origin_of(url: str) -> str:
    """规范化 HTTP(S) origin；拒绝凭据、控制字符和非网络协议。"""
    try:
        if (
            len(url) > 8192
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
            or "\\" in url
        ):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.port == 0:
            raise ValueError
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = "" if port == (443 if parts.scheme == "https" else 80) else f":{port}"
        return f"{parts.scheme}://{host}{suffix}"
    except ValueError:
        raise BrowserError("network_denied", "浏览器仅接受无凭据的 HTTP(S) 站点。") from None


@dataclass(frozen=True)
class BrowserPolicy:
    """显式 origin 白名单；不是 DNS 固定或操作系统网络沙箱。"""

    allowed_origins: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise ValueError("浏览器必须由宿主指定允许站点。")
        normalized = set()
        for origin in self.allowed_origins:
            parts = urlsplit(origin)
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                raise ValueError("浏览器白名单必须是 origin，不是页面路径。")
            normalized.add(origin_of(origin))
        object.__setattr__(self, "allowed_origins", frozenset(normalized))

    def check(self, url: str) -> None:
        """拒绝未授权站点；重定向和子资源也必须逐请求调用。"""
        if origin_of(url) not in self.allowed_origins:
            raise BrowserError("network_denied", "浏览器站点不在宿主允许范围；未自动扩大权限。")

"""URL 与已解析地址的网络策略；测试放行只能由可信宿主注入。"""

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from adam.web.models import WebError


@dataclass(frozen=True)
class NetworkPolicy:
    """生产仅访问公共 HTTP(S)；测试例外精确到环回地址、协议和端口。"""

    local_test_origins: frozenset[str] = frozenset()

    def normalize(self, url: str) -> str:
        """拒绝凭据、控制字符和非 HTTP URL，去掉不发送给服务端的片段。"""
        if not url or len(url) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise WebError("invalid_url", "网页 URL 无效或超长。")
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
            if (
                parts.scheme not in {"http", "https"}
                or not host
                or parts.username is not None
                or parts.password is not None
                or "%" in host
                or "\\" in parts.netloc
            ):
                raise ValueError
            host = host.encode("idna").decode("ascii").lower()
            authority = f"[{host}]" if ":" in host else host
            if port is not None:
                authority += f":{port}"
            normalized = urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
        except (ValueError, UnicodeError):
            raise WebError("invalid_url", "仅支持不含凭据的 HTTP(S) URL。") from None
        origin = f"{parts.scheme}://{authority}"
        if port not in (None, 80, 443) and origin not in self.local_test_origins:
            raise WebError("network_denied", "网页网络策略拒绝此端口。")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return normalized
        self.check_addresses(normalized, (host,))
        return normalized

    def check_addresses(self, url: str, addresses: tuple[str, ...]) -> None:
        """每次实际连接前检查全部 DNS 结果；混合公私地址也拒绝。"""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if not addresses:
            raise WebError("network_error", "网页地址无法解析。")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if origin in self.local_test_origins and ip.is_loopback:
                continue
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise WebError("network_denied", "网页网络策略拒绝非公共地址。")

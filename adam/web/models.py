"""网页读取的本地值对象与失败分类。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError


class WebError(ToolError):
    """保留安全的失败分类，不把远端响应、凭据或系统异常写入提示。"""

    def __init__(self, code: str, message: str) -> None:
        self.kind = code
        super().__init__(message)


@dataclass(frozen=True)
class FetchLimits:
    """一次完整抓取的资源上限；秒数包含 DNS、重定向和读取。"""

    max_bytes: int = 1048576
    max_chars: int = 16000
    max_redirects: int = 4
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        for name, maximum in (("max_bytes", 10000000), ("max_chars", 1000000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} 超出允许范围。")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects 必须为 0 到 10。")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds 必须大于 0 且不超过 120。")


@dataclass(frozen=True)
class PageResponse:
    """网络适配器返回的有界实体；重定向由服务逐跳处理。"""

    status: int
    body: bytes = b""
    content_type: str = ""
    location: str | None = None


@dataclass(frozen=True)
class WebPage:
    """一次抓取版本；摘要对应解码前实体，正文截断不改变摘要。"""

    requested_url: str
    final_url: str
    fetched_at: str
    sha256: str
    byte_size: int
    title: str
    text: str
    content_type: str
    truncated: bool
    redirects: tuple[str, ...]
    schema_version: int = 1


class PageTransport(Protocol):
    """能力服务借用的可取消网络端口；所有者负责关闭。"""

    def read(
        self, url: str, *, remaining_seconds: float, cancel: CancellationToken
    ) -> PageResponse:
        """读取一个已规范化 URL；连接前验证 DNS 并固定地址，不自动重定向。"""
        ...

    def close(self) -> None:
        """释放网络资源；关闭不受已取消令牌阻止。"""
        ...

"""固定已验证 DNS 地址的 HTTP 后端；不使用宿主代理、cookies 或隐式重试。"""

import asyncio
import socket
from contextlib import suppress
from urllib.parse import urlsplit

import httpx

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.web.models import FetchLimits, PageResponse, WebError
from adam.web.policy import NetworkPolicy


class HttpPageTransport:
    """在所属调用线程拥有事件循环与连接资源，不跨线程共享。"""

    def __init__(
        self,
        policy: NetworkPolicy,
        limits: FetchLimits,
        *,
        content_types: frozenset[str] = frozenset(
            {"text/html", "text/plain", "application/xhtml+xml"}
        ),
        user_agent: str = "Adam-Web/1",
    ) -> None:
        """固定响应类型和客户端标识；默认仍只接受网页，JSON 须由可信宿主显式注入。"""
        if not content_types or not content_types <= {
            "text/html",
            "text/plain",
            "application/xhtml+xml",
            "application/json",
        }:
            raise ValueError("HTTP 响应类型配置无效。")
        if (
            not user_agent
            or len(user_agent) > 200
            or any(ord(char) < 32 or ord(char) > 126 for char in user_agent)
        ):
            raise ValueError("HTTP 客户端标识无效。")
        self._content_types = content_types
        self._user_agent = user_agent
        self._policy = policy
        self._limits = limits
        self._runner = asyncio.Runner()
        self._closed = False
        # 连接池按 IP 分组，不允许不同域名复用同一 TLS 连接。
        try:
            self._client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        except BaseException:
            self._runner.close()
            raise

    def read(
        self, url: str, *, remaining_seconds: float, cancel: CancellationToken
    ) -> PageResponse:
        """有界读取实体；取消真正的异步 I/O，不仅停止同步等待。"""
        if self._closed:
            raise WebError("closed", "网页服务已关闭。")
        cancel.check()
        return self._runner.run(self._wait(url, remaining_seconds, cancel))

    def close(self) -> None:
        """关闭连接池和事件循环；重复关闭无副作用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._runner.run(self._client.aclose())
        finally:
            self._runner.close()

    async def _wait(self, url: str, seconds: float, cancel: CancellationToken) -> PageResponse:
        """轮询停止令牌并取消在途任务；清理后仍传播原始中断。"""
        task = asyncio.create_task(self._read(url, seconds))
        try:
            while not task.done():
                cancel.check()
                await asyncio.wait({task}, timeout=0.05)
            cancel.check()
            return task.result()
        except asyncio.CancelledError:
            raise RequestInterrupted() from None
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _read(self, url: str, seconds: float) -> PageResponse:
        """将连接地址替换为已校验 IP；Host 和 TLS SNI 仍使用原域名。"""
        try:
            async with asyncio.timeout(seconds):
                url = self._policy.normalize(url)
                parts = urlsplit(url)
                host = parts.hostname
                assert host is not None
                port = parts.port or (443 if parts.scheme == "https" else 80)
                records = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
                addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
                self._policy.check_addresses(url, addresses)
                target = httpx.URL(url).copy_with(host=addresses[0])
                # 不复用跨域 cookies，也不让第二次 DNS 查询改变实际连接目标。
                async with self._client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": parts.netloc,
                        "Accept-Encoding": "identity",
                        "User-Agent": self._user_agent,
                        "Accept": ", ".join(sorted(self._content_types)),
                    },
                    extensions={"sni_hostname": host},
                    timeout=seconds,
                ) as response:
                    self._client.cookies.clear()
                    if response.status_code != 200:
                        return PageResponse(
                            response.status_code, location=response.headers.get("location")
                        )
                    kind = response.headers.get("content-type", "")
                    if kind.split(";", 1)[0].strip().lower() not in self._content_types:
                        raise WebError("unsupported_type", "远端实体类型不在允许范围内。")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise WebError("unsupported_encoding", "网页未遵守无压缩读取约束。")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > self._limits.max_bytes:
                            raise WebError("too_large", "网页实体超过字节上限。")
                        body.extend(chunk)
                    return PageResponse(response.status_code, bytes(body), kind)
        except (TimeoutError, httpx.TimeoutException):
            raise WebError("timeout", "网页抓取超时。") from None
        except (httpx.HTTPError, OSError):
            raise WebError("network_error", "网页网络连接失败。") from None

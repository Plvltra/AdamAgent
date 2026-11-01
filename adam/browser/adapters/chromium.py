"""Playwright Chromium 后端；独占临时无头上下文，不使用日常浏览器配置。"""

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, TypeVar
from urllib.parse import urljoin
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    Playwright,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from adam.browser.adapters.dom import SNAPSHOT_SCRIPT
from adam.browser.models import BrowserError, DomSnapshot, ObservationLimits
from adam.browser.policy import BrowserPolicy
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted

T = TypeVar("T")


class ChromiumDriver:
    """在创建线程持有事件循环；超时/取消后禁用，不隐式重放页面请求。"""

    def __init__(
        self,
        policy: BrowserPolicy,
        limits: ObservationLimits,
        *,
        channel: str | None = None,
        cancel: CancellationToken | None = None,
    ) -> None:
        self._policy, self._limits = policy, limits
        self._runner = asyncio.Runner()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._closed = self._failed = False
        self._blocked_requests = 0
        self._blocked_navigation = False
        self._navigating = False
        self._route_timeout = False
        self._redirect: str | None = None
        self._key = "__adam_" + uuid4().hex
        try:
            self._run(self._start(channel), cancel or CancellationToken())
        except BaseException as error:
            try:
                self.close()
            except Exception:
                error.add_note("浏览器创建失败后的资源清理失败；未重试。")
            raise

    @property
    def engine_version(self) -> str:
        """返回实际启动的 Chromium 版本，供运行证据使用。"""
        return self._browser.version if self._browser else ""

    def navigate(self, url: str, cancel: CancellationToken) -> None:
        """打开白名单页面，等待 DOMContentLoaded；动态内容由后续 observe 获取。"""
        self._policy.check(url)
        self._run(self._navigate(url), cancel)

    def snapshot(self, cancel: CancellationToken) -> DomSnapshot:
        """读取有界文字与控件摘要，不导出图片、存储、cookies 或输入值。"""
        return self._run(self._snapshot(), cancel)

    def is_current(self, snapshot: DomSnapshot, element_id: str, cancel: CancellationToken) -> bool:
        """验证原节点及观察版本；被替换的同名控件不会获得旧引用。"""
        return self._run(self._is_current(snapshot, element_id), cancel)

    def close(self) -> None:
        """依次关闭上下文、浏览器与 SDK，失败仍尝试剩余清理；重复调用无副作用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._runner.run(self._close())
        finally:
            self._runner.close()

    def _run(self, operation: Coroutine[Any, Any, T], cancel: CancellationToken) -> T:
        """把 SDK 超时/故障转成受控失败；中断后不允许复用不确定的页面状态。"""
        if self._closed or self._failed or cancel.cancelled:
            operation.close()
            cancel.check()
            raise BrowserError("closed", "浏览器已关闭或发生故障，请重新创建。")
        try:
            return self._runner.run(self._wait(operation, cancel))
        except asyncio.CancelledError:
            self._failed = True
            raise RequestInterrupted() from None
        except (TimeoutError, PlaywrightTimeoutError):
            self._failed = True
            raise BrowserError("timeout", "浏览器操作超时；未自动重试。") from None
        except Error:
            self._failed = True
            kind = (
                "network_denied"
                if self._blocked_navigation
                else ("timeout" if self._route_timeout else "browser_failed")
            )
            raise BrowserError(kind, "浏览器操作失败；未返回有效观察或自动重试。") from None

    async def _wait(self, operation: Coroutine[Any, Any, T], cancel: CancellationToken) -> T:
        """取消实际在途协程，并等待它停止后才返回同步调用者。"""
        task = asyncio.create_task(operation)
        try:
            async with asyncio.timeout(self._limits.timeout_seconds):
                while not task.done():
                    if cancel.cancelled:
                        raise asyncio.CancelledError
                    await asyncio.wait({task}, timeout=0.05)
                if cancel.cancelled:
                    raise asyncio.CancelledError
                return task.result()
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _start(self, channel: str | None) -> None:
        """启动临时上下文，阻止下载、Service Worker、WebSocket 和未授权 HTTP 请求。"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel=channel,
            headless=True,
            args=["--no-proxy-server", "--disable-background-networking"],
        )
        self._context = await self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            permissions=[],
        )
        await self._context.route("**/*", self._route)
        await self._context.route_web_socket("**/*", self._block_socket)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._limits.timeout_seconds * 1000)
        self._page.set_default_navigation_timeout(self._limits.timeout_seconds * 1000)

    async def _route(self, route: Route) -> None:
        """逐请求校验；主页面重定向交还 navigate，其他重定向不自动跟随。

        Playwright 只路由链首请求，故不能把 3xx 交给浏览器自动跟随。
        """
        main = bool(
            route.request.is_navigation_request()
            and self._page
            and route.request.frame == self._page.main_frame
        )
        response = None
        try:
            self._policy.check(route.request.url)
            if route.request.method not in {"GET", "HEAD"}:
                raise BrowserError("network_denied", "观察入口不允许提交请求。")
            response = await route.fetch(
                max_redirects=0, max_retries=0, timeout=self._limits.timeout_seconds * 1000
            )
            if 300 <= response.status < 400:
                target = urljoin(route.request.url, response.headers.get("location", ""))
                self._policy.check(target)
                if not main or not self._navigating or not response.headers.get("location"):
                    raise BrowserError("network_denied", "子资源或非显式打开的重定向已拒绝。")
                self._redirect = target
                await route.abort("aborted")
            else:
                await route.fulfill(response=response)
        except BrowserError:
            self._blocked_requests += 1
            if main:
                self._blocked_navigation = True
            with suppress(Error):
                await route.abort("blockedbyclient")
        except Error as error:
            if main and isinstance(error, PlaywrightTimeoutError):
                self._route_timeout = True
            with suppress(Error):
                await route.abort("failed")
        finally:
            if response is not None:
                with suppress(Error):
                    await response.dispose()

    async def _block_socket(self, socket: WebSocketRoute) -> None:
        self._blocked_requests += 1
        await socket.close()

    async def _navigate(self, url: str) -> None:
        """同一期限内手动打开每跳，确保重定向后的请求仍经过路由。"""
        assert self._page is not None
        self._blocked_navigation = False
        self._route_timeout = False
        self._navigating = True
        visited = set()
        try:
            for _ in range(6):
                if url in visited:
                    raise BrowserError("redirect_limit", "浏览器重定向循环；未返回有效观察。")
                visited.add(url)
                self._redirect = None
                try:
                    response = await self._page.goto(url, wait_until="domcontentloaded")
                except Error:
                    if self._redirect is None:
                        raise
                    url = self._redirect
                    continue
                if response is None or response.status >= 400:
                    raise BrowserError("http_error", "浏览器页面没有成功响应；未返回有效观察。")
                self._policy.check(self._page.url)
                return
            raise BrowserError("redirect_limit", "浏览器重定向超过上限；未返回有效观察。")
        finally:
            self._navigating = False
            self._redirect = None

    async def _snapshot(self) -> DomSnapshot:
        assert self._page is not None
        data = await self._page.evaluate(
            SNAPSHOT_SCRIPT,
            {
                "key": self._key,
                "mode": "observe",
                "maxChars": self._limits.max_chars,
                "maxElements": self._limits.max_elements,
            },
        )
        return DomSnapshot(
            data["documentId"],
            data["revision"],
            data["url"],
            data["title"],
            data["text"],
            tuple(tuple(item) for item in data["elements"]),
            data["textTruncated"],
            data["elementsTruncated"],
            self._blocked_requests,
        )

    async def _is_current(self, snapshot: DomSnapshot, element_id: str) -> bool:
        assert self._page is not None
        return bool(
            await self._page.evaluate(
                SNAPSHOT_SCRIPT,
                {
                    "key": self._key,
                    "mode": "validate",
                    "expected": {
                        "documentId": snapshot.document_id,
                        "revision": snapshot.revision,
                        "elementId": element_id,
                    },
                },
            )
        )

    async def _close(self) -> None:
        """资源关闭不依赖页面存活；清理故障不会阻止 SDK 驱动停止。"""
        errors = []
        # 先移除路由会放行尚在等待的原请求，取消时可能造成第二次真实发送。
        # 保持拦截到上下文关闭，在途 fetch/fulfill 的关闭竞态由路由处理器收尾。
        # 实测已安装 Chrome 的正常退出可达约 16 秒；与短的上下文清理分开计时。
        for resource, timeout in ((self._context, 5), (self._browser, 30)):
            if resource is not None:
                try:
                    await asyncio.wait_for(resource.close(), timeout=timeout)
                except Exception as error:
                    errors.append(error)
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=5)
            except Exception as error:
                errors.append(error)
        if errors:
            raise BrowserError("cleanup_failed", "浏览器资源清理失败，请检查残留进程。")

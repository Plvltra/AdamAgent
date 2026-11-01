"""Playwright Chromium 后端；独占临时无头上下文，不使用日常浏览器配置。"""

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, TypeVar
from urllib.parse import urldefrag, urljoin
from uuid import uuid4

from playwright.async_api import (
    APIResponse,
    Browser,
    BrowserContext,
    Dialog,
    ElementHandle,
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
from adam.browser.models import (
    BrowserAction,
    BrowserDownload,
    BrowserError,
    BrowserTab,
    DomSnapshot,
    ElementReference,
    ObservationLimits,
)
from adam.browser.policy import BrowserPolicy
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.contracts.media import validate_mime_type

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
        self._pages: dict[str, Page] = {}
        self._opening_tab = False
        self._pending_popup_urls: list[str] = []
        self._dismissed_dialogs = 0
        self._download_requested = False
        self._download_result: BrowserDownload | None = None
        self._download_error: BrowserError | None = None
        self._download_ready: asyncio.Event | None = None
        self._closed = self._failed = False
        self._blocked_requests = 0
        self._blocked_navigation = False
        self._navigating = False
        self._acting = False
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
        self._run(self._open(url), cancel)

    def snapshot(self, cancel: CancellationToken) -> DomSnapshot:
        """读取有界文字与控件摘要，不导出图片、存储、cookies 或输入值。"""
        return self._run(self._snapshot(), cancel)

    def is_current(self, snapshot: DomSnapshot, element_id: str, cancel: CancellationToken) -> bool:
        """验证原节点及观察版本；被替换的同名控件不会获得旧引用。"""
        return self._run(self._is_current(snapshot, element_id), cancel)

    def act(self, snapshot: DomSnapshot, action: BrowserAction, cancel: CancellationToken) -> bool:
        """仅执行固定的原生控件操作；执行前再次核对原节点。"""
        return self._run(self._act(snapshot, action), cancel)

    def tabs(self, cancel: CancellationToken) -> tuple[BrowserTab, ...]:
        """列出所属标签，不暴露 Playwright Page 或触发导航。"""
        return self._run(self._tabs(), cancel)

    def select_tab(self, tab_id: str, cancel: CancellationToken) -> None:
        """选择仍存活的原标签；不按标题找替代页面。"""
        self._run(self._select_tab(tab_id), cancel)

    def close_tab(self, tab_id: str, cancel: CancellationToken) -> None:
        """关闭确切标签，不运行 beforeunload；其他页面不自动获得当前身份。"""
        self._run(self._close_tab(tab_id), cancel)

    def link_target(self, snapshot: DomSnapshot, element_id: str, cancel: CancellationToken) -> str:
        """以固定 DOM 程序验证原链接并读取实际目标，不执行导航。"""
        return self._run(self._link_target(snapshot, element_id), cancel)

    def download(
        self, snapshot: DomSnapshot, reference: ElementReference, cancel: CancellationToken
    ) -> BrowserDownload:
        """原链接单次点击后收集完整附件；浏览器不取得任意宿主保存路径。"""
        return self._run(self._download(snapshot, reference), cancel)

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
        """启动临时上下文；下载仅走显式收集，阻止 Service Worker/WebSocket/未授权请求。"""
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
        self._context.on("page", self._on_page)
        await self._new_page()

    async def _new_page(self) -> None:
        """只由启动或显式 open 创建标签，分配新身份且不驱逐已有页面。"""
        assert self._context is not None
        self._opening_tab = True
        try:
            page = await self._context.new_page()
            if not self._remember(page):
                raise BrowserError("resource_limit", "浏览器标签数量已达上限。")
            self._page = page
        finally:
            self._opening_tab = False

    def _remember(self, page: Page) -> bool:
        """只收录创建窗口或已批准动作期间出现的标签；原始页面对象对应固定身份。"""
        if page in self._pages.values():
            return True
        pending = urldefrag(page.url)[0] in self._pending_popup_urls
        if (
            not (self._opening_tab or self._acting or pending)
            or len(self._pages) >= self._limits.max_tabs
        ):
            return False
        if pending:
            self._pending_popup_urls.remove(urldefrag(page.url)[0])
        tab_id = uuid4().hex
        self._pages[tab_id] = page
        page.set_default_timeout(self._limits.timeout_seconds * 1000)
        page.set_default_navigation_timeout(self._limits.timeout_seconds * 1000)
        page.on("dialog", self._dismiss_dialog)
        page.on("close", lambda _page: self._forget(tab_id, page))
        page.on("crash", self._crashed)
        return True

    async def _on_page(self, page: Page) -> None:
        """只认领获准动作的白名单弹窗；不符合策略的页面关闭，不作为新权限。"""
        if page in self._pages.values():
            return
        if self._opening_tab:
            self._remember(page)
            return
        try:
            await self._popup_loaded(page)
            self._policy.check(page.url)
            if self._remember(page):
                return
        except (BrowserError, Error):
            pass
        self._blocked_requests += 1
        with suppress(Error):
            await page.close(run_before_unload=False)

    async def _popup_loaded(self, page: Page) -> None:
        """等待原弹窗导航和 DOM，不通过重开 URL 补偿延后的页面交付。"""
        if self._pending_popup_urls:
            await page.wait_for_url(
                lambda url: (
                    not self._pending_popup_urls
                    or urldefrag(str(url))[0] in self._pending_popup_urls
                ),
                wait_until="domcontentloaded",
                timeout=self._limits.timeout_seconds * 1000,
            )
        else:
            await page.wait_for_load_state("domcontentloaded")

    def _forget(self, tab_id: str, page: Page) -> None:
        self._pages.pop(tab_id, None)
        if self._page is page:
            self._page = None

    def _crashed(self, page: Page) -> None:
        # 页面崩溃不证明刚才的 POST 未被接受，整个资源停止复用。
        self._failed = True

    async def _dismiss_dialog(self, dialog: Dialog) -> None:
        """拒绝 confirm/prompt/beforeunload，关闭 alert；不把对话框文本当作新批准。"""
        self._dismissed_dialogs += 1
        with suppress(Error):
            await dialog.dismiss()

    async def _tabs(self) -> tuple[BrowserTab, ...]:
        result = []
        for tab_id, page in tuple(self._pages.items()):
            if page.is_closed():
                continue
            try:
                self._policy.check(page.url)
                title = await page.title()
            except BrowserError:
                with suppress(Error):
                    await page.close(run_before_unload=False)
                continue
            except Error:
                continue
            result.append(
                BrowserTab(
                    tab_id,
                    page.url,
                    title.encode("utf-8", errors="replace").decode("utf-8")[:300],
                    page is self._page,
                )
            )
        return tuple(result)

    def _find_tab(self, tab_id: str) -> Page:
        page = self._pages.get(tab_id)
        if page is None or page.is_closed():
            raise BrowserError("stale_reference", "标签已关闭或不属于当前上下文。")
        self._policy.check(page.url)
        return page

    async def _select_tab(self, tab_id: str) -> None:
        self._page = self._find_tab(tab_id)

    async def _close_tab(self, tab_id: str) -> None:
        page = self._find_tab(tab_id)
        await page.close(run_before_unload=False)
        self._forget(tab_id, page)

    async def _route(self, route: Route) -> None:
        """逐请求校验；主页面重定向交还 navigate/动作入口，其他重定向不自动跟随。

        Playwright 只路由链首请求，故不能把 3xx 交给浏览器自动跟随。
        """
        main = active = False
        response = None
        try:
            self._policy.check(route.request.url)
            try:
                page = route.request.frame.page
                main = (
                    route.request.is_navigation_request() and route.request.frame == page.main_frame
                )
                active = page is self._page
            except Error:
                # Chromium 的弹窗首请求先于 Frame/Page 创建；只为本次批准动作保留有界名额。
                if not (
                    self._acting
                    and route.request.is_navigation_request()
                    and route.request.method == "GET"
                ):
                    raise BrowserError("network_denied", "无所属页面的请求已拒绝。") from None
                if len(self._pages) + len(self._pending_popup_urls) >= self._limits.max_tabs:
                    raise BrowserError("resource_limit", "弹窗数量超过当前上下文上限。") from None
                page, main = None, True
                self._pending_popup_urls.append(route.request.url)
            if (
                page is not None
                and page not in self._pages.values()
                and not (main and self._remember(page))
            ):
                raise BrowserError("network_denied", "未批准的新标签或弹窗请求已拒绝。")
            if route.request.method not in {"GET", "HEAD"} and not (
                self._acting and active and route.request.method == "POST"
            ):
                raise BrowserError("network_denied", "未批准的观察入口不允许提交请求。")
            response = await route.fetch(
                max_redirects=0, max_retries=0, timeout=self._limits.timeout_seconds * 1000
            )
            if 300 <= response.status < 400:
                target = urljoin(route.request.url, response.headers.get("location", ""))
                self._policy.check(target)
                if (
                    not (main and active)
                    or not (self._navigating or self._acting)
                    or not response.headers.get("location")
                ):
                    raise BrowserError("network_denied", "子资源或非显式打开的重定向已拒绝。")
                if self._acting and response.status not in {301, 302, 303}:
                    raise BrowserError(
                        "network_denied", "不自动重提表单或跟随保留方法的动作重定向。"
                    )
                self._redirect = target
                await route.abort("aborted")
            elif (
                response.headers.get("content-disposition", "").split(";", 1)[0].strip().lower()
                == "attachment"
            ):
                if (
                    not (main and active and self._download_requested)
                    or self._download_result is not None
                ):
                    raise BrowserError("download_denied", "附件须通过显式下载工具逐次批准。")
                await self._capture_attachment(response)
                # 不把附件交给浏览器磁盘下载，也不使用服务器提供的 filename 作为路径。
                await route.abort("aborted")
            else:
                await route.fulfill(response=response)
        except BrowserError as error:
            if self._download_requested and active and main:
                self._download_error = error
                if self._download_ready is not None:
                    self._download_ready.set()
            self._blocked_requests += 1
            if main and active:
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

    async def _capture_attachment(self, response: APIResponse) -> None:
        """收集完整附件并检查交付上限；MIME 是声明，文件名不成为宿主路径。"""
        raw = await response.body()
        if response.status >= 400 or len(raw) > self._limits.max_download_bytes:
            raise BrowserError("download_failed", "下载响应失败或超过完整正文上限。")
        mime = (
            response.headers.get("content-type", "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        try:
            validate_mime_type(mime)
        except ValueError:
            raise BrowserError("download_failed", "下载响应的 MIME 无效。") from None
        self._download_result = BrowserDownload(raw, mime)
        assert self._download_ready is not None
        self._download_ready.set()

    async def _block_socket(self, socket: WebSocketRoute) -> None:
        self._blocked_requests += 1
        await socket.close()

    async def _open(self, url: str) -> None:
        """仅显式 open 可在最后一个标签关闭后创建新标签。"""
        if self._page is None:
            await self._new_page()
        await self._navigate(url)

    async def _navigate(self, url: str) -> None:
        """同一期限内在原标签手动打开每跳，仍经路由；页面丢失不新建。"""
        if self._page is None:
            raise BrowserError("no_active_tab", "当前标签已丢失；未自动打开重定向。")
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
                    if self._download_requested and self._download_result is not None:
                        return
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
        if self._page is None:
            raise BrowserError("no_active_tab", "没有当前标签；请显式选择标签或打开页面。")
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
            next(identity for identity, page in self._pages.items() if page is self._page),
            self._dismissed_dialogs,
        )

    async def _is_current(self, snapshot: DomSnapshot, element_id: str) -> bool:
        if self._page is None or self._pages.get(snapshot.tab_id) is not self._page:
            return False
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

    async def _link_target(self, snapshot: DomSnapshot, element_id: str) -> str:
        if self._page is None or self._pages.get(snapshot.tab_id) is not self._page:
            raise BrowserError("stale_reference", "下载链接不属于当前标签。")
        value = await self._page.evaluate(
            SNAPSHOT_SCRIPT,
            {
                "key": self._key,
                "mode": "target",
                "expected": {
                    "documentId": snapshot.document_id,
                    "revision": snapshot.revision,
                    "elementId": element_id,
                },
            },
        )
        if not isinstance(value, str):
            raise BrowserError("stale_reference", "下载链接已失效，请重新观察。")
        return value

    async def _act(self, snapshot: DomSnapshot, action: BrowserAction) -> bool:
        """通过固定程序取得原节点句柄，不按选择器重找；动作后只跟随 GET 结果页。"""
        if self._page is None or self._pages.get(snapshot.tab_id) is not self._page:
            return False
        handle = await self._page.evaluate_handle(
            SNAPSHOT_SCRIPT,
            {
                "key": self._key,
                "mode": "handle",
                "expected": {
                    "documentId": snapshot.document_id,
                    "revision": snapshot.revision,
                    "elementId": action.reference.element_id,
                },
            },
        )
        element = handle.as_element()
        try:
            if element is None:
                return False
            self._redirect = None
            self._blocked_navigation = False
            self._acting = True
            try:
                if action.operation == "click":
                    await self._click(element)
                elif action.operation == "fill":
                    assert action.value is not None
                    await element.fill(action.value)
                elif action.operation == "select":
                    assert action.value is not None
                    await element.select_option(value=action.value)
                else:
                    await element.evaluate(
                        "(element, delta) => element.scrollBy(0, delta)", action.scroll_y
                    )
            except Error:
                if self._redirect is None and not self._download_requested:
                    raise
            if self._blocked_navigation and not self._download_requested:
                raise BrowserError(
                    "network_denied", "动作导航被站点策略拒绝；实际副作用可能已发生。"
                )
            if self._redirect is not None:
                await self._navigate(self._redirect)
            if self._pending_popup_urls:
                await self._settle_popups()
            return True
        finally:
            self._acting = False
            self._redirect = None
            with suppress(Error, TimeoutError):
                await asyncio.wait_for(handle.dispose(), timeout=1)

    async def _click(self, element: ElementHandle) -> None:
        """先订阅弹窗；点击完成后才计事件窗口，避免可操作性等待耗尽弹窗许可。"""
        assert self._page is not None
        popup = asyncio.create_task(self._page.wait_for_event("popup", timeout=0))
        await asyncio.sleep(0)
        try:
            await element.click()
            try:
                await asyncio.wait_for(popup, timeout=0.5)
            except TimeoutError:
                pass
        finally:
            if not popup.done():
                popup.cancel()
            with suppress(asyncio.CancelledError, Error):
                await popup

    async def _settle_popups(self) -> None:
        """等待已放行首请求对应的 Page 交付；不再次导航，不把事件延后误判为没有弹窗。"""
        assert self._context is not None
        while self._pending_popup_urls:
            candidates = [
                page
                for page in self._context.pages
                if page is not self._page and page not in self._pages.values()
            ]
            page = candidates[0] if candidates else await self._context.wait_for_event("page")
            await self._popup_loaded(page)
            if not self._remember(page):
                await page.close(run_before_unload=False)
                raise BrowserError("resource_limit", "已批准弹窗没有可用名额，已关闭。")

    async def _download(
        self, snapshot: DomSnapshot, reference: ElementReference
    ) -> BrowserDownload:
        """收集当前点击的附件响应；无附件、超限或取消都不交付残片或自动再次点击。"""
        self._download_requested = True
        self._download_result = self._download_error = None
        self._download_ready = asyncio.Event()
        try:
            if not await self._act(snapshot, BrowserAction("click", reference)):
                raise BrowserError("stale_reference", "下载派发前原链接已失效。")
            await self._download_ready.wait()
            if self._download_error is not None:
                raise self._download_error
            assert self._download_result is not None
            return self._download_result
        finally:
            self._download_requested = False
            self._download_result = self._download_error = self._download_ready = None

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

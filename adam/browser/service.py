"""协调浏览器观察及引用时效，不负责 SDK、用户批准或会话存储。"""

from datetime import UTC, datetime
from uuid import uuid4

from adam.browser.models import (
    BrowserAction,
    BrowserDownload,
    BrowserDriver,
    BrowserError,
    BrowserObservation,
    BrowserTab,
    DomSnapshot,
    ElementReference,
    ObservedElement,
)
from adam.browser.policy import BrowserPolicy
from adam.contracts.browser import BrowserActionUnknownError, BrowserApproval
from adam.contracts.cancellation import CancellationToken


class BrowserObserver:
    """单线程上下文观察；每次观察、切换或关闭使旧引用失效，关闭权属于创建后端的宿主。"""

    def __init__(self, driver: BrowserDriver, policy: BrowserPolicy) -> None:
        self._driver = driver
        self._policy = policy
        self._browser_id = uuid4().hex
        self._last: BrowserObservation | None = None
        self._snapshot: DomSnapshot | None = None

    def open(self, url: str, *, cancel: CancellationToken | None = None) -> BrowserObservation:
        """打开获准 URL 并观察 DOM；失败不返回旧快照、不重试或等待网络空闲。"""
        token = cancel or CancellationToken()
        token.check()
        self._policy.check(url)
        self._last = self._snapshot = None
        self._driver.navigate(url, token)
        return self.observe(cancel=token)

    def observe(self, *, cancel: CancellationToken | None = None) -> BrowserObservation:
        """读取此刻 DOM；动态页面可再次观察，旧观察的引用立即失效。"""
        token = cancel or CancellationToken()
        token.check()
        self._last = self._snapshot = None
        snapshot = self._driver.snapshot(token)
        self._policy.check(snapshot.url)
        observation_id = uuid4().hex
        observation = BrowserObservation(
            self._browser_id,
            snapshot.tab_id,
            observation_id,
            snapshot.document_id,
            snapshot.revision,
            snapshot.url,
            snapshot.title,
            snapshot.text,
            tuple(
                ObservedElement(
                    ElementReference(self._browser_id, snapshot.tab_id, observation_id, identifier),
                    tag,
                    role,
                    name,
                    input_type,
                )
                for identifier, tag, role, name, input_type in snapshot.elements
            ),
            snapshot.text_truncated,
            snapshot.elements_truncated,
            datetime.now(UTC).isoformat(),
            snapshot.blocked_requests,
            dismissed_dialogs=snapshot.dismissed_dialogs,
        )
        self._last, self._snapshot = observation, snapshot
        return observation

    def inspect_reference(
        self, reference: ElementReference, *, cancel: CancellationToken | None = None
    ) -> ObservedElement:
        """校验已观察控件仍是当前元素；失效须重新观察，不执行点击或输入。

        本次检查不是未来动作的授权或原子保证，动作入口仍须在执行时再次校验。
        """
        token = cancel or CancellationToken()
        token.check()
        last, snapshot = self._last, self._snapshot
        element = (
            next((item for item in last.elements if item.reference == reference), None)
            if last
            else None
        )
        if element is None or snapshot is None:
            raise BrowserError("stale_reference", "控件引用不属于当前观察，请重新观察页面。")
        if not self._driver.is_current(snapshot, reference.element_id, token):
            self._last = self._snapshot = None
            raise BrowserError("stale_reference", "页面或控件已变化，请重新观察页面。")
        return element

    def plan_action(
        self, action: BrowserAction, *, cancel: CancellationToken | None = None
    ) -> BrowserApproval:
        """验证当前引用与动作类型，形成不可变批准对象；不在服务内决定用户批准。"""
        element = self.inspect_reference(action.reference, cancel=cancel)
        if element.input_type.lower() in {"password", "file"}:
            raise BrowserError("unsupported_control", "首版不操作密码或文件控件。")
        if action.operation == "fill" and element.tag not in {"input", "textarea"}:
            raise BrowserError("unsupported_control", "fill 仅支持原生 input 或 textarea。")
        if action.operation == "select" and element.tag != "select":
            raise BrowserError("unsupported_control", "select 仅支持原生 select。")
        assert self._last is not None
        ref = action.reference
        return BrowserApproval(
            action.operation,
            self._last.url,
            element.name,
            (ref.browser_id, ref.tab_id, ref.observation_id, ref.element_id),
            action.value,
            action.scroll_y,
        )

    def perform(
        self, action: BrowserAction, *, cancel: CancellationToken | None = None
    ) -> BrowserObservation | None:
        """批准后重新核对并派发；返回新观察，观察失败不要求重做已派发动作。

        调用者须先批准 plan_action 的确切对象。本服务是可信宿主 API，不是模型直通入口。
        SDK 故障可能已有副作用，抛 BrowserActionUnknownError 停止轮次；取消继续传播原中断。
        """
        token = cancel or CancellationToken()
        self.plan_action(action, cancel=token)
        snapshot = self._snapshot
        assert snapshot is not None
        self._last = self._snapshot = None
        try:
            dispatched = self._driver.act(snapshot, action, token)
        except (BrowserError, OSError, ValueError):
            raise BrowserActionUnknownError(
                "浏览器动作结果未知，已停止；请人工检查页面，不要自动重做。"
            ) from None
        if not dispatched:
            raise BrowserError("stale_reference", "动作派发前页面已变化，请重新观察。")
        try:
            return self.observe(cancel=token)
        except (BrowserError, OSError, ValueError):
            return None

    def tabs(self, *, cancel: CancellationToken | None = None) -> tuple[BrowserTab, ...]:
        """返回所属上下文的标签；不新建、不切换，也不使当前控件引用失效。"""
        return self._driver.tabs(cancel or CancellationToken())

    @property
    def browser_id(self) -> str:
        """返回当前资源身份；关闭重建后的身份不会复用。"""
        return self._browser_id

    def plan_tab(
        self, operation: str, browser_id: str, tab_id: str, *, cancel: CancellationToken
    ) -> BrowserApproval:
        """固定切换/关闭的确切页面；调用方批准后还须比较新的计划。"""
        if browser_id != self._browser_id:
            raise BrowserError("stale_reference", "标签不属于当前浏览器。")
        tab = next((item for item in self.tabs(cancel=cancel) if item.tab_id == tab_id), None)
        if tab is None or operation not in {"select_tab", "close_tab"}:
            raise BrowserError("stale_reference", "标签已关闭或不属于当前上下文。")
        return BrowserApproval(operation, tab.url, tab.title, browser_id=browser_id, tab_id=tab_id)

    def perform_tab(self, plan: BrowserApproval, *, cancel: CancellationToken) -> None:
        """批准后复检完整页面计划；不默认选择替代页面或恢复旧控件引用。"""
        if (
            self.plan_tab(plan.operation, plan.browser_id or "", plan.tab_id or "", cancel=cancel)
            != plan
        ):
            raise BrowserError("stale_reference", "批准期间标签已变化，未切换或关闭。")
        self._last = self._snapshot = None
        try:
            if plan.operation == "select_tab":
                self._driver.select_tab(plan.tab_id or "", cancel)
            else:
                self._driver.close_tab(plan.tab_id or "", cancel)
        except (BrowserError, OSError, ValueError):
            raise BrowserActionUnknownError(
                "标签操作结果未确认，已停止；不自动重开或重复关闭。"
            ) from None

    def plan_download(
        self, reference: ElementReference, *, cancel: CancellationToken
    ) -> BrowserApproval:
        """只允许原观察中的链接；下载是单独批准，不继承普通 click 的许可。"""
        element = self.inspect_reference(reference, cancel=cancel)
        if element.tag != "a":
            raise BrowserError("unsupported_control", "下载只支持已观察的原生链接。")
        assert self._snapshot is not None
        target = self._driver.link_target(self._snapshot, reference.element_id, cancel)
        self._policy.check(target)
        return BrowserApproval(
            "download",
            target,
            element.name,
            (
                reference.browser_id,
                reference.tab_id,
                reference.observation_id,
                reference.element_id,
            ),
        )

    def download(
        self, reference: ElementReference, *, cancel: CancellationToken
    ) -> BrowserDownload:
        """复检原链接后派发一次下载；失败可能已有网络副作用，停止且不重试。

        可信调用方须先批准 plan_download 的确切对象；本服务不自行授予下载权限。
        """
        self.plan_download(reference, cancel=cancel)
        snapshot = self._snapshot
        assert snapshot is not None
        self._last = self._snapshot = None
        try:
            return self._driver.download(snapshot, reference, cancel)
        except (BrowserError, OSError, ValueError):
            raise BrowserActionUnknownError(
                "下载未形成有效媒体证据，已停止；不要自动重新下载或提交。"
            ) from None

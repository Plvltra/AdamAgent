"""协调浏览器观察及引用时效，不负责 SDK、用户批准或会话存储。"""

from datetime import UTC, datetime
from uuid import uuid4

from adam.browser.models import (
    BrowserDriver,
    BrowserError,
    BrowserObservation,
    DomSnapshot,
    ElementReference,
    ObservedElement,
)
from adam.browser.policy import BrowserPolicy
from adam.contracts.cancellation import CancellationToken


class BrowserObserver:
    """单线程、单标签观察；每次观察替换引用，关闭权属于创建后端的宿主。"""

    def __init__(self, driver: BrowserDriver, policy: BrowserPolicy) -> None:
        self._driver = driver
        self._policy = policy
        self._browser_id = uuid4().hex
        self._tab_id = uuid4().hex
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
            self._tab_id,
            observation_id,
            snapshot.document_id,
            snapshot.revision,
            snapshot.url,
            snapshot.title,
            snapshot.text,
            tuple(
                ObservedElement(
                    ElementReference(self._browser_id, self._tab_id, observation_id, identifier),
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

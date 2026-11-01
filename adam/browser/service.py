"""协调浏览器观察及引用时效，不负责 SDK、用户批准或会话存储。"""

from datetime import UTC, datetime
from uuid import uuid4

from adam.browser.models import (
    BrowserAction,
    BrowserDriver,
    BrowserError,
    BrowserObservation,
    DomSnapshot,
    ElementReference,
    ObservedElement,
)
from adam.browser.policy import BrowserPolicy
from adam.contracts.browser import BrowserActionUnknownError, BrowserApproval
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

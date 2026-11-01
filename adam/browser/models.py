"""浏览器观察快照、资源上限与窄后端端口。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError


class BrowserError(ToolError):
    """向调用者暴露受控类别，不泄露浏览器异常或页面凭据。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class ObservationLimits:
    """限制返回文字、控件数量及每次操作耗时；不等于浏览器进程内存上限。"""

    max_chars: int = 16000
    max_elements: int = 100
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        for name, maximum in (("max_chars", 100000), ("max_elements", 500)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} 超出允许范围。")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds 必须大于 0 且不超过 120。")


@dataclass(frozen=True)
class ElementReference:
    """仅对一个浏览器、标签及一次观察有效；不是 CSS 选择器或执行授权。"""

    browser_id: str
    tab_id: str
    observation_id: str
    element_id: str


@dataclass(frozen=True)
class ObservedElement:
    """可见且未禁用的控件摘要；不返回输入值、密码或 cookies。"""

    reference: ElementReference
    tag: str
    role: str
    name: str
    input_type: str


@dataclass(frozen=True)
class BrowserObservation:
    """一个有界 DOM 版本；页面文字均是不可信资料，不授予权限。"""

    browser_id: str
    tab_id: str
    observation_id: str
    document_id: str
    revision: int
    url: str
    title: str
    text: str
    elements: tuple[ObservedElement, ...]
    text_truncated: bool
    elements_truncated: bool
    observed_at: str
    blocked_requests: int
    schema_version: int = 1


@dataclass(frozen=True)
class DomSnapshot:
    """后端返回的 DOM 数据；引用身份由业务服务补齐。"""

    document_id: str
    revision: int
    url: str
    title: str
    text: str
    elements: tuple[tuple[str, str, str, str, str], ...]
    text_truncated: bool
    elements_truncated: bool
    blocked_requests: int


class BrowserDriver(Protocol):
    """调用线程借用后端；创建方关闭，业务服务不接触 SDK 对象。"""

    def navigate(self, url: str, cancel: CancellationToken) -> None:
        """打开已获宿主许可的页面；禁止隐式重试。"""
        ...

    def snapshot(self, cancel: CancellationToken) -> DomSnapshot:
        """读取最新 DOM 并替换后端元素引用表。"""
        ...

    def is_current(self, snapshot: DomSnapshot, element_id: str, cancel: CancellationToken) -> bool:
        """检查文档、DOM 版本及原始控件身份，不按选择器匹配替代元素。"""
        ...

    def close(self) -> None:
        """释放浏览器及驱动资源；可重复调用，不受取消令牌阻止。"""
        ...

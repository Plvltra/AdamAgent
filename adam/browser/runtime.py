"""独立浏览器资源入口；可选 SDK 仅在真实使用时加载。"""

from collections.abc import Iterator
from contextlib import contextmanager

from adam.browser.models import BrowserError, ObservationLimits
from adam.browser.policy import BrowserPolicy
from adam.browser.service import BrowserObserver
from adam.contracts.cancellation import CancellationToken


@contextmanager
def open_browser(
    policy: BrowserPolicy,
    limits: ObservationLimits | None = None,
    *,
    channel: str | None = None,
    cancel: CancellationToken | None = None,
) -> Iterator[BrowserObserver]:
    """在当前线程独占临时浏览器；创建失败/取消均清理，保留原始业务故障。

    channel 由可信宿主选择已安装的 chrome/ msedge，省略则使用 Playwright Chromium。
    此入口尚不注册模型工具，不提供任意 JavaScript、点击或上传能力。
    """
    if cancel is not None:
        cancel.check()
    try:
        from adam.browser.adapters.chromium import ChromiumDriver
    except ImportError:
        raise BrowserError(
            "backend_missing", "浏览器依赖未安装，请安装 browser 可选依赖。"
        ) from None
    driver = ChromiumDriver(policy, limits or ObservationLimits(), channel=channel, cancel=cancel)
    try:
        yield BrowserObserver(driver, policy)
    except BaseException as error:
        try:
            driver.close()
        except Exception:
            error.add_note("浏览器资源清理失败；原始错误保留，未重试页面请求。")
        raise
    else:
        driver.close()

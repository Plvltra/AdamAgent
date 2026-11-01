"""浏览器可信配置、SDK 隔离及资源失败边界。"""

import json
import subprocess
import sys

import pytest

from adam.browser.models import BrowserError, ObservationLimits
from adam.browser.policy import BrowserPolicy, origin_of
from adam.browser.runtime import open_browser


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "http://user:secret@example.com/",
        "http://example.com:bad",
        "http://example.com:0/",
        "http://example.com/" + "x" * 8192,
        "http://example.com/\n",
        "http://example.com\\evil",
    ],
)
def test_invalid_url_never_passes_origin_policy(url):
    with pytest.raises(BrowserError) as caught:
        origin_of(url)
    assert caught.value.kind == "network_denied"


def test_origin_policy_is_exact_and_normalizes_default_ports():
    policy = BrowserPolicy(frozenset({"https://Example.com:443/", "http://[::1]:88"}))
    policy.check("https://example.com/path?query=1#fragment")
    policy.check("http://[::1]:88/path")
    for url in (
        "http://example.com",
        "https://evil.example.com",
        "https://example.com:444",
        "https://example.com.evil",
    ):
        with pytest.raises(BrowserError):
            policy.check(url)
    with pytest.raises(ValueError):
        BrowserPolicy(frozenset())
    with pytest.raises(ValueError):
        BrowserPolicy(frozenset({"https://example.com/path"}))


@pytest.mark.parametrize(
    "values",
    [
        {"max_chars": 0},
        {"max_elements": 0},
        {"max_chars": True},
        {"max_elements": 501},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
    ],
)
def test_resource_limits_reject_invalid_configuration(values):
    with pytest.raises(ValueError):
        ObservationLimits(**values)


def test_import_and_disabled_normal_bootstrap_do_not_need_playwright():
    code = """
import importlib.abc, json, sys
class NoBrowser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'playwright':
            raise ImportError('optional backend absent')
sys.meta_path.insert(0, NoBrowser())
import adam.bootstrap, adam.browser.runtime
from adam.browser.policy import BrowserPolicy
from adam.browser.models import BrowserError
try:
    with adam.browser.runtime.open_browser(BrowserPolicy(frozenset({'https://example.com'}))):
        raise AssertionError('missing backend was accepted')
except BrowserError as error:
    assert error.kind == 'backend_missing'
print(json.dumps([name for name in sys.modules if name.startswith('playwright')]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_startup_failure_still_closes_sdk_driver(monkeypatch):
    """故障注入浏览器启动边界；驱动已启动但创建 Chromium 失败，仍必须 stop。"""
    from adam.browser.adapters.chromium import ChromiumDriver

    stopped = []

    class Engine:
        async def launch(self, **kwargs):
            raise BrowserError("launch_failed", "fixture launch failure")

    class SDK:
        chromium = Engine()

        async def stop(self):
            stopped.append(True)

    class Starter:
        async def start(self):
            return SDK()

    monkeypatch.setattr("adam.browser.adapters.chromium.async_playwright", Starter)
    with pytest.raises(BrowserError) as caught:
        ChromiumDriver(BrowserPolicy(frozenset({"https://example.com"})), ObservationLimits())
    assert caught.value.kind == "launch_failed" and stopped == [True]


def test_cleanup_error_does_not_replace_body_failure(monkeypatch):
    """故障注入 close 边界；保留原业务错误，不宣称已成功清理。"""

    class Driver:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            raise BrowserError("cleanup_failed", "fixture close failure")

    monkeypatch.setattr("adam.browser.adapters.chromium.ChromiumDriver", Driver)
    with pytest.raises(ValueError, match="original") as caught:
        with open_browser(BrowserPolicy(frozenset({"https://example.com"}))):
            raise ValueError("original")
    assert "清理失败" in caught.value.__notes__[0]

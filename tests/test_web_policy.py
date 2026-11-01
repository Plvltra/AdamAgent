"""网页 URL/DNS 策略和资源参数的协议组合，不重复业务 HTTP 场景。"""

import pytest

from adam.web.models import FetchLimits, WebError
from adam.web.policy import NetworkPolicy


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "ftp://example.com/a",
        "http://user:pass@example.com/",
        "http://example.com:99999/",
        "http://example.com/\n",
        "http://example.com/a b",
        "http://[fe80::1%25eth0]/",
        "http://example.com\\@other.com/",
        "",
    ],
)
def test_invalid_url_rejected_without_echoing_secrets(url):
    with pytest.raises(WebError) as caught:
        NetworkPolicy().normalize(url)
    assert caught.value.kind == "invalid_url"
    assert "pass" not in str(caught.value)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_private_and_special_dns_addresses_are_denied(address):
    with pytest.raises(WebError) as caught:
        NetworkPolicy().check_addresses("https://example.com/", (address,))
    assert caught.value.kind == "network_denied"


def test_mixed_dns_denied_and_test_exception_cannot_allow_non_loopback():
    policy = NetworkPolicy(frozenset({"http://test.invalid:9123"}))
    with pytest.raises(WebError):
        policy.check_addresses("http://test.invalid:9123/", ("8.8.8.8", "10.0.0.1"))
    with pytest.raises(WebError):
        policy.check_addresses("http://test.invalid:9123/", ("10.0.0.1",))
    with pytest.raises(WebError):
        policy.check_addresses("http://other.invalid:9123/", ("127.0.0.1",))
    policy.check_addresses("http://test.invalid:9123/", ("127.0.0.1",))
    NetworkPolicy().check_addresses("https://example.com/", ("8.8.8.8", "1.1.1.1"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_chars": 1000001},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"max_redirects": -1},
        {"max_redirects": True},
    ],
)
def test_fetch_limits_reject_unbounded_or_ambiguous_values(kwargs):
    with pytest.raises(ValueError):
        FetchLimits(**kwargs)

"""C01.1：真实本地 HTTP 验证正文、来源、重定向、边界与实际取消。"""

import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.web.adapters.http import HttpPageTransport
from adam.web.models import FetchLimits, WebError
from adam.web.policy import NetworkPolicy
from adam.web.service import WebReader


@contextmanager
def reader_at(origin, **overrides):
    limits = replace(FetchLimits(), **overrides)
    policy = NetworkPolicy(frozenset({origin}))
    transport = HttpPageTransport(policy, limits)
    try:
        yield WebReader(transport, policy, limits)
    finally:
        transport.close()
        transport.close()


def test_fetch_keeps_bounded_visible_body_and_versioned_provenance(website, webpage_html):
    origin, requests, _, _ = website
    with reader_at(origin) as reader:
        page = reader.fetch(origin + "/redirect#not-sent")
    assert page.schema_version == 1
    assert page.requested_url == origin + "/redirect"
    assert page.final_url == origin + "/page"
    assert page.redirects == (origin + "/page",)
    assert page.title == "Local & verified" and page.text == "Report Version one"
    assert page.sha256 == hashlib.sha256(webpage_html).hexdigest()
    assert page.byte_size == len(webpage_html) and not page.truncated
    assert datetime.fromisoformat(page.fetched_at).tzinfo is not None
    assert [item[0] for item in requests] == ["/redirect", "/page"]
    assert all(item[1]["Host"] == origin.split("//")[1] for item in requests)


def test_text_bound_does_not_claim_complete_or_change_source_digest(website, webpage_html):
    origin = website[0]
    with reader_at(origin, max_chars=6) as reader:
        page = reader.fetch(origin + "/page")
    assert page.text == "Report" and page.truncated
    assert page.sha256 == hashlib.sha256(webpage_html).hexdigest()


@pytest.mark.parametrize(
    "path,code,limits",
    [
        ("/large", "too_large", {"max_bytes": 64}),
        ("/error", "http_503", {}),
        ("/binary", "unsupported_type", {}),
        ("/encoding", "unsupported_encoding", {}),
        ("/charset", "unsupported_charset", {}),
        ("/loop", "redirect_loop", {}),
        ("/hop", "redirect_limit", {"max_redirects": 1}),
        ("/missing-location", "invalid_redirect", {}),
        ("/forbidden", "network_denied", {}),
        ("/slow", "timeout", {"timeout_seconds": 0.15}),
    ],
)
def test_failures_are_distinct_no_retry_or_valid_snapshot(website, path, code, limits):
    origin, requests, _, _ = website
    with reader_at(origin, **limits) as reader, pytest.raises(WebError) as caught:
        reader.fetch(origin + path)
    assert caught.value.kind == code
    assert "secret" not in str(caught.value)
    assert [item[0] for item in requests].count(path) == 1
    if path == "/forbidden":
        assert len(requests) == 1


def test_plain_unicode_and_invalid_utf8_are_explicitly_decoded(website):
    with reader_at(website[0]) as reader:
        assert reader.fetch(website[0] + "/plain").text == "本地正文"
        assert reader.fetch(website[0] + "/bad-utf8").text == "bad\ufffdtext"


def test_redirect_does_not_forward_cookies_or_host_proxy_settings(website, monkeypatch):
    origin, requests, _, _ = website
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    with reader_at(origin) as reader:
        reader.fetch(origin + "/cookie")
    assert len(requests) == 2
    assert all("Cookie" not in item[1] and "Authorization" not in item[1] for item in requests)


def test_cancel_interrupts_actual_pending_body_and_closes_transport(website):
    origin, requests, ready, _ = website
    cancel = CancellationToken()
    stopped = threading.Event()
    observed = []

    def run():
        try:
            with reader_at(origin) as reader:
                reader.fetch(origin + "/slow", cancel=cancel)
        except RequestInterrupted:
            observed.append("interrupted")
        finally:
            stopped.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert ready.wait(3)
    start = time.monotonic()
    cancel.cancel()
    assert stopped.wait(2)
    worker.join(2)
    assert time.monotonic() - start < 2
    assert observed == ["interrupted"] and len(requests) == 1


def test_already_cancelled_does_not_send_any_request(website):
    origin, requests, _, _ = website
    cancel = CancellationToken()
    cancel.cancel()
    with reader_at(origin) as reader, pytest.raises(RequestInterrupted):
        reader.fetch(origin + "/page", cancel=cancel)
    assert requests == []


def test_dns_rebinding_cannot_reresolve_the_validated_hostname(website, monkeypatch):
    """故障注入 DNS 边界；第二次解析同一域名会换地址，实际连接仍须命中首个地址。"""
    origin, requests, _, _ = website
    fake_origin = origin.replace("127.0.0.1", "fixture.invalid")
    original = socket.getaddrinfo
    lookups = []

    def resolve(host, port, *args, **kwargs):
        if host in ("fixture.invalid", b"fixture.invalid"):
            lookups.append(host)
            host = "127.0.0.1" if len(lookups) == 1 else "127.0.0.2"
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with reader_at(fake_origin) as reader:
        page = reader.fetch(fake_origin + "/page")
    assert page.final_url == fake_origin + "/page" and len(lookups) == 1
    assert requests[0][1]["Host"] == fake_origin.split("//")[1]


def test_default_policy_denies_local_server_before_connecting(website):
    origin, requests, _, _ = website
    limits = FetchLimits()
    policy = NetworkPolicy()
    transport = HttpPageTransport(policy, limits)
    try:
        with pytest.raises(WebError, match="网络策略"):
            WebReader(transport, policy, limits).fetch(origin + "/page")
        assert requests == []
    finally:
        transport.close()


def test_standalone_cli_reports_policy_failure_without_a_model_or_credentials():
    completed = subprocess.run(
        [sys.executable, "-m", "adam.web", "http://user:secret@example.com/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert result["error_type"] == "invalid_url" and not result["ok"]
    assert "secret" not in completed.stdout and completed.stderr == ""


def test_dns_failure_is_classified_and_not_retried(website, monkeypatch):
    origin, requests, _, _ = website
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise socket.gaierror("private resolver diagnostics")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with reader_at(origin) as reader, pytest.raises(WebError) as caught:
        reader.fetch(origin + "/page")
    assert caught.value.kind == "network_error" and len(calls) == 1
    assert "private" not in str(caught.value) and requests == []


def test_closed_transport_rejects_without_network(website):
    origin, requests, _, _ = website
    policy, limits = NetworkPolicy(frozenset({origin})), FetchLimits()
    transport = HttpPageTransport(policy, limits)
    transport.close()
    with pytest.raises(WebError) as caught:
        WebReader(transport, policy, limits).fetch(origin + "/page")
    assert caught.value.kind == "closed" and requests == []

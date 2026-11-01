"""C01.2：真实本地 HTTP 验证百科候选、故障分类、网络隔离与取消。"""

import hashlib
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.web import search as search_cli
from adam.web.adapters.http import HttpPageTransport
from adam.web.adapters.wikipedia import WikipediaSearch
from adam.web.models import FetchLimits, WebError
from adam.web.policy import NetworkPolicy
from adam.web.runtime import open_search
from adam.web.search_models import SearchLimits
from adam.web.service import WebReader


def search_body(title="Local encyclopedia"):
    return json.dumps(
        {
            "query": {
                "search": [
                    {
                        "ns": 0,
                        "pageid": 42,
                        "title": title,
                        "snippet": '<span class="searchmatch">Local</span> &amp; verified<script>hidden secret</script>',
                        "url": "http://169.254.169.254/private",
                    }
                ]
            },
            "continue": {"sroffset": 1, "continue": "-||"},
        },
        ensure_ascii=False,
    ).encode()


@pytest.fixture
def encyclopedia():
    """只替代远端 API；HTTP、读取/取消和响应解析均执行真实本地流程。"""
    requests = []
    ready, release = threading.Event(), threading.Event()
    state = {"title": "Local encyclopedia"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            query = parse_qs(urlsplit(self.path).query).get("srsearch", [""])[0]
            body = search_body(state["title"])
            status, content_type, extra = 200, "application/json; charset=utf-8", {}
            if query == "empty":
                body = b'{"query":{"search":[]}}'
            elif query == "rate":
                status, body = 429, b"private remote secret"
            elif query == "api-rate":
                body = b'{"error":{"code":"ratelimited","info":"private remote secret"}}'
            elif query == "api-error":
                body = b'{"error":{"code":"readapidenied","info":"private remote secret"}}'
            elif query == "warning":
                body = b'{"warnings":{"search":"private remote secret"},"query":{"search":[]}}'
            elif query == "http":
                status, body = 503, b"private remote secret"
            elif query in ("redirect", "remote-redirect"):
                status = 302
                extra["Location"] = (
                    "/followed" if query == "redirect" else "http://169.254.169.254/private"
                )
            elif query == "empty-body":
                body = b""
            elif query == "bad-json":
                body = b'{"private secret":'
            elif query == "bad-schema":
                body = b'{"query":{"search":[{"title":"private secret"}]}}'
            elif query == "html":
                content_type = "text/html"
            elif query == "gzip":
                extra["Content-Encoding"] = "gzip"
            elif query == "large":
                body = b"x" * 4096
            extra["Set-Cookie"] = "private=secret"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if query == "slow":
                ready.set()
                release.wait(5)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, ready, release, state
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


@contextmanager
def search_at(origin, **overrides):
    limits = replace(SearchLimits(), **overrides)
    policy = NetworkPolicy(frozenset({origin}))
    transport = HttpPageTransport(
        policy,
        FetchLimits(max_bytes=limits.max_bytes, timeout_seconds=limits.timeout_seconds),
        content_types=frozenset({"application/json"}),
    )
    try:
        yield WikipediaSearch(transport, policy, limits, endpoint=origin + "/w/api.php")
    finally:
        transport.close()
        transport.close()


def test_search_returns_bounded_untrusted_candidates_not_page_evidence(encyclopedia):
    origin, requests, _, _, _ = encyclopedia
    with search_at(origin) as backend:
        result = backend.search("  中文 & + query  ")
    assert result.provider == "wikipedia" and result.evidence_kind == "search_snippets"
    assert result.scope == "English Wikipedia encyclopedia; not general web search"
    assert result.query == "中文 & + query" and result.has_more and result.schema_version == 1
    assert result.response_sha256 == hashlib.sha256(search_body()).hexdigest()
    assert result.response_byte_size == len(search_body())
    assert datetime.fromisoformat(result.searched_at).tzinfo is not None
    (candidate,) = result.candidates
    assert candidate.title == "Local encyclopedia" and candidate.snippet == "Local & verified"
    assert candidate.url == origin + "/w/index.php?curid=42"
    assert not candidate.title_truncated and not candidate.snippet_truncated
    assert len(requests) == 1 and urlsplit(requests[0][0]).path == "/w/api.php"
    params = parse_qs(urlsplit(requests[0][0]).query)
    assert params["srsearch"] == [result.query] and params["srlimit"] == ["5"]
    assert params["srnamespace"] == ["0"] and params["srprop"] == ["snippet"]
    assert requests[0][1]["Accept"] == "application/json"


def test_title_and_snippet_limits_are_explicit_without_changing_response_identity(encyclopedia):
    with search_at(
        encyclopedia[0], max_results=1, max_title_chars=5, max_snippet_chars=6
    ) as backend:
        result = backend.search("bounded")
    (candidate,) = result.candidates
    assert candidate.title == "Local" and candidate.title_truncated
    assert candidate.snippet == "Local " and candidate.snippet_truncated
    assert result.response_sha256 == hashlib.sha256(search_body()).hexdigest()
    assert parse_qs(urlsplit(encyclopedia[1][0][0]).query)["srlimit"] == ["1"]


def test_legitimate_empty_result_is_success_not_configuration_or_http_failure(encyclopedia):
    with search_at(encyclopedia[0]) as backend:
        result = backend.search("empty")
    assert result.candidates == () and not result.has_more
    assert result.response_byte_size > 0 and len(encyclopedia[1]) == 1


def test_each_search_returns_its_actual_response_version_without_stale_fallback(encyclopedia):
    with search_at(encyclopedia[0]) as backend:
        first = backend.search("version")
        encyclopedia[4]["title"] = "Changed title"
        second = backend.search("version")
        with pytest.raises(WebError) as caught:
            backend.search("http")
    assert first.response_sha256 != second.response_sha256
    assert first.candidates[0].title == "Local encyclopedia"
    assert second.candidates[0].title == "Changed title"
    assert caught.value.kind == "http_503" and len(encyclopedia[1]) == 3


@pytest.mark.parametrize(
    "query,code,limits",
    [
        ("rate", "rate_limited", {}),
        ("api-rate", "rate_limited", {}),
        ("api-error", "api_error", {}),
        ("warning", "api_warning", {}),
        ("http", "http_503", {}),
        ("redirect", "unexpected_redirect", {}),
        ("remote-redirect", "unexpected_redirect", {}),
        ("empty-body", "invalid_response", {}),
        ("bad-json", "invalid_response", {}),
        ("bad-schema", "invalid_response", {}),
        ("html", "unsupported_type", {}),
        ("gzip", "unsupported_encoding", {}),
        ("large", "too_large", {"max_bytes": 64}),
        ("slow", "timeout", {"timeout_seconds": 0.15}),
    ],
)
def test_faults_never_return_empty_success_retry_or_follow_redirects(
    encyclopedia, query, code, limits
):
    with search_at(encyclopedia[0], **limits) as backend, pytest.raises(WebError) as caught:
        backend.search(query)
    assert caught.value.kind == code and "secret" not in str(caught.value)
    assert len(encyclopedia[1]) == 1


def test_search_isolated_from_host_proxies_authentication_and_response_cookies(
    encyclopedia, monkeypatch
):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    with search_at(encyclopedia[0]) as backend:
        backend.search("one")
        backend.search("two")
    assert len(encyclopedia[1]) == 2
    assert all(
        "Cookie" not in headers and "Authorization" not in headers for _, headers in encyclopedia[1]
    )


def test_actual_pending_search_can_be_cancelled_and_resources_released(encyclopedia):
    token = CancellationToken()
    stopped, errors = threading.Event(), []

    def run():
        try:
            with search_at(encyclopedia[0]) as backend:
                backend.search("slow", cancel=token)
        except RequestInterrupted:
            errors.append("interrupted")
        finally:
            stopped.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert encyclopedia[2].wait(3)
    start = time.monotonic()
    token.cancel()
    assert stopped.wait(2)
    worker.join(2)
    assert time.monotonic() - start < 2 and errors == ["interrupted"]
    assert len(encyclopedia[1]) == 1


def test_already_cancelled_search_does_not_send_any_request(encyclopedia):
    token = CancellationToken()
    token.cancel()
    with search_at(encyclopedia[0]) as backend, pytest.raises(RequestInterrupted):
        backend.search("cancelled", cancel=token)
    assert encyclopedia[1] == []


def test_production_network_policy_does_not_inherit_local_test_permission(encyclopedia):
    with pytest.raises(WebError) as caught:
        WikipediaSearch(
            None, NetworkPolicy(), SearchLimits(), endpoint=encyclopedia[0] + "/w/api.php"
        )
    assert caught.value.kind == "network_denied" and encyclopedia[1] == []


def test_json_search_transport_does_not_make_json_a_readable_webpage(encyclopedia):
    origin = encyclopedia[0]
    limits = FetchLimits()
    policy = NetworkPolicy(frozenset({origin}))
    for content_types in (None, frozenset({"application/json"})):
        options = {} if content_types is None else {"content_types": content_types}
        transport = HttpPageTransport(policy, limits, **options)
        try:
            with pytest.raises(WebError) as caught:
                WebReader(transport, policy, limits).fetch(origin + "/w/api.php")
            assert caught.value.kind == "unsupported_type"
        finally:
            transport.close()
    assert len(encyclopedia[1]) == 2


def test_closed_search_transport_never_sends_a_second_request(encyclopedia):
    origin = encyclopedia[0]
    policy = NetworkPolicy(frozenset({origin}))
    transport = HttpPageTransport(
        policy, FetchLimits(), content_types=frozenset({"application/json"})
    )
    backend = WikipediaSearch(transport, policy, SearchLimits(), endpoint=origin + "/w/api.php")
    backend.search("before close")
    transport.close()
    with pytest.raises(WebError) as caught:
        backend.search("after close")
    assert caught.value.kind == "closed" and len(encyclopedia[1]) == 1


def test_missing_backend_does_not_create_transport_or_treat_it_as_no_results(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("missing configuration must not allocate network resources")

    monkeypatch.setattr("adam.web.runtime.HttpPageTransport", unexpected)
    with pytest.raises(WebError) as caught, open_search(None, SearchLimits(), NetworkPolicy()):
        pytest.fail("must not enter unconfigured service")
    assert caught.value.kind == "not_configured"


def test_cli_runs_real_local_search_without_creating_a_model_or_session(
    encyclopedia, monkeypatch, capsys
):
    @contextmanager
    def local_factory(backend, limits, policy):
        assert backend == "wikipedia" and limits.max_results == 1
        assert policy.local_test_origins == frozenset()
        with search_at(encyclopedia[0], max_results=limits.max_results) as local:
            yield local

    monkeypatch.setattr(search_cli, "open_search", local_factory)
    monkeypatch.setattr(sys, "argv", ["search", "local", "--backend", "wikipedia", "--limit", "1"])
    assert search_cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and len(result["candidates"]) == 1 and len(encyclopedia[1]) == 1
    assert result["evidence_kind"] == "search_snippets"


def test_standalone_cli_reports_missing_backend_without_credentials():
    completed = subprocess.run(
        [sys.executable, "-m", "adam.web.search", "query"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 1 and not result["ok"] and completed.stderr == ""
    assert result["error_type"] == "not_configured"

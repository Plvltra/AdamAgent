"""搜索协议组合与资源工厂边界；HTTP 业务闭环另由 features 验收。"""

import json
import time
from dataclasses import replace

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.web import runtime
from adam.web.adapters.http import HttpPageTransport
from adam.web.adapters.wikipedia import WikipediaSearch
from adam.web.models import FetchLimits, PageResponse, WebError
from adam.web.policy import NetworkPolicy
from adam.web.search_models import SearchLimits


def valid_entry(**changes):
    return {"ns": 0, "pageid": 1, "title": "Wikipedia", "snippet": "A result", **changes}


class ResponseTransport:
    """仅替代响应/关闭边界，供非法协议和清理故障组合使用。"""

    def __init__(self, body=None, *, response=None, close_error=False, delay=0):
        self.response = response or PageResponse(
            200, body if body is not None else b'{"query":{"search":[]}}', "application/json"
        )
        self.calls, self.closes = [], 0
        self.close_error, self.delay = close_error, delay

    def read(self, url, *, remaining_seconds, cancel):
        cancel.check()
        self.calls.append((url, remaining_seconds))
        time.sleep(self.delay)
        return self.response

    def close(self):
        self.closes += 1
        if self.close_error:
            raise OSError("private close diagnostic")


def backend_with(data):
    transport = ResponseTransport(json.dumps(data).encode())
    return WikipediaSearch(transport, NetworkPolicy(), SearchLimits()), transport


@pytest.mark.parametrize(
    "entry",
    [
        None,
        [],
        {},
        valid_entry(pageid=True),
        valid_entry(pageid=0),
        valid_entry(pageid=-1),
        valid_entry(pageid=2**63),
        valid_entry(pageid="1"),
        valid_entry(ns=True),
        valid_entry(ns=1),
        valid_entry(title=None),
        valid_entry(title=" "),
        valid_entry(title="bad\ud800"),
        valid_entry(snippet=[]),
        valid_entry(snippet="bad\x01"),
        valid_entry(snippet="bad\ud800"),
    ],
)
def test_invalid_candidate_discards_entire_batch_not_just_the_bad_entry(entry):
    backend, transport = backend_with({"query": {"search": [valid_entry(pageid=2), entry]}})
    with pytest.raises(WebError) as caught:
        backend.search("query")
    assert caught.value.kind == "invalid_response" and len(transport.calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        {"query": None},
        {"query": {"search": {}}},
        {"query": {"search": [valid_entry(), valid_entry()]}},
        {"query": {"search": [valid_entry(pageid=i + 1) for i in range(6)]}},
        {"query": {"search": []}, "continue": []},
        {"query": {"search": []}, "continue": None},
        {"query": {"search": []}, "continue": {"sroffset": True, "continue": "x"}},
        {"query": {"search": []}, "continue": {"sroffset": -1, "continue": "x"}},
        {"query": {"search": []}, "continue": {"sroffset": 1, "continue": 1}},
    ],
)
def test_invalid_response_shape_duplicate_id_or_excess_candidates_is_not_empty_success(data):
    backend, _ = backend_with(data)
    with pytest.raises(WebError) as caught:
        backend.search("query")
    assert caught.value.kind == "invalid_response"


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":{"search":[]},"query":{"search":[]}}',
        b'{"query":{"search":[]},"n":NaN}',
        b'{"query":{"search":[]},"n":Infinity}',
        b"[" * 2000 + b"]" * 2000,
        b'{"query":{"search":[]},"n":"\xff"}',
    ],
)
def test_non_json_ambiguous_or_excessively_nested_payload_is_a_safe_failure(body):
    backend = WikipediaSearch(ResponseTransport(body), NetworkPolicy(), SearchLimits())
    with pytest.raises(WebError) as caught:
        backend.search("query")
    assert caught.value.kind == "invalid_response"


def test_inline_highlights_do_not_insert_spaces_inside_a_word():
    backend, _ = backend_with(
        {"query": {"search": [valid_entry(snippet="Wiki<span>pe</span>dia<br>next")]}}
    )
    assert backend.search("word").candidates[0].snippet == "Wikipedia next"


@pytest.mark.parametrize(
    "query", ["", " ", "a\nb", "a\tb", "bad\x7f", "bad\ud800", "x" * 513, "中" * 500]
)
def test_invalid_or_url_encoded_excessive_query_never_sends_a_request(query):
    transport = ResponseTransport()
    backend = WikipediaSearch(transport, NetworkPolicy(), SearchLimits())
    with pytest.raises(WebError) as caught:
        backend.search(query)
    assert caught.value.kind == "invalid_query" and transport.calls == []


@pytest.mark.parametrize(
    "limits",
    [
        {"max_results": True},
        {"max_results": 0},
        {"max_results": 21},
        {"max_query_chars": 1025},
        {"max_title_chars": 0},
        {"max_snippet_chars": 4001},
        {"max_bytes": 10000001},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
    ],
)
def test_search_limits_reject_unbounded_or_ambiguous_values(limits):
    with pytest.raises(ValueError):
        SearchLimits(**limits)


@pytest.mark.parametrize("suffix", ["?secret=value", "#fragment"])
def test_host_configured_endpoint_cannot_smuggle_extra_request_parameters(suffix):
    with pytest.raises(ValueError):
        WikipediaSearch(
            ResponseTransport(),
            NetworkPolicy(),
            SearchLimits(),
            endpoint="https://example.com/w/api.php" + suffix,
        )


@pytest.mark.parametrize(
    "response,code",
    [
        (PageResponse(200, b"x" * 30, "application/json"), "too_large"),
        (PageResponse(200, b"{}", "text/html"), "unsupported_type"),
        (PageResponse(403, b"secret", "application/json"), "http_403"),
    ],
)
def test_service_keeps_byte_and_type_guards_even_when_transport_does_not_enforce_them(
    response, code
):
    backend = WikipediaSearch(
        ResponseTransport(response=response), NetworkPolicy(), SearchLimits(max_bytes=20)
    )
    with pytest.raises(WebError) as caught:
        backend.search("query")
    assert caught.value.kind == code and "secret" not in str(caught.value)


def test_deadline_includes_work_after_transport_has_returned():
    """端口故障注入：忽略剩余期限，晚到的合法响应仍不得成为成功。"""
    backend = WikipediaSearch(
        ResponseTransport(delay=0.03), NetworkPolicy(), SearchLimits(timeout_seconds=0.01)
    )
    with pytest.raises(WebError) as caught:
        backend.search("query")
    assert caught.value.kind == "timeout"


@pytest.mark.parametrize("failure", ["request", "cancel", "constructor"])
def test_runtime_releases_resources_and_preserves_original_failure_when_close_also_fails(
    monkeypatch, failure
):
    transport = ResponseTransport(response=PageResponse(503), close_error=True)
    monkeypatch.setattr(runtime, "HttpPageTransport", lambda *args, **kwargs: transport)
    expected = WebError("construction_failed", "safe failure")
    if failure == "constructor":

        def fail(*args, **kwargs):
            raise expected

        monkeypatch.setattr(runtime, "WikipediaSearch", fail)
    token = CancellationToken()
    if failure == "cancel":
        token.cancel()
    with pytest.raises(RequestInterrupted if failure == "cancel" else WebError) as caught:
        with runtime.open_search("wikipedia", SearchLimits(), NetworkPolicy()) as backend:
            backend.search("query", cancel=token)
    assert transport.closes == 1
    assert "原始错误保留" in caught.value.__notes__[0]
    if failure == "constructor":
        assert caught.value is expected and transport.calls == []
    elif failure == "cancel":
        assert transport.calls == []
    else:
        assert caught.value.kind == "http_503" and len(transport.calls) == 1


def test_runtime_success_owns_json_only_resource_and_closes_once(monkeypatch):
    transport, options = ResponseTransport(), []

    def factory(policy, limits, **kwargs):
        options.append((policy, limits, kwargs))
        return transport

    monkeypatch.setattr(runtime, "HttpPageTransport", factory)
    limits = replace(SearchLimits(), max_bytes=200, timeout_seconds=3)
    with runtime.open_search("wikipedia", limits, NetworkPolicy()) as backend:
        assert backend.search("query").candidates == ()
    assert transport.closes == 1 and len(transport.calls) == 1
    assert options[0][1].max_bytes == 200 and options[0][1].timeout_seconds == 3
    assert options[0][2]["content_types"] == frozenset({"application/json"})
    assert options[0][2]["user_agent"].startswith("AdamAgentPractice/")


def test_unknown_backend_is_rejected_before_allocating_http_resources(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("must not allocate transport")

    monkeypatch.setattr(runtime, "HttpPageTransport", unexpected)
    with (
        pytest.raises(WebError) as caught,
        runtime.open_search("unknown", SearchLimits(), NetworkPolicy()),
    ):
        pytest.fail("must not enter")
    assert caught.value.kind == "unsupported_backend"


@pytest.mark.parametrize(
    "options",
    [
        {"content_types": frozenset()},
        {"content_types": frozenset({"image/png"})},
        {"user_agent": ""},
        {"user_agent": "bad\r\nheader"},
        {"user_agent": "中文"},
    ],
)
def test_trusted_transport_options_are_still_bounded_before_creating_async_resources(options):
    with pytest.raises(ValueError):
        HttpPageTransport(NetworkPolicy(), FetchLimits(), **options)


def test_other_public_service_cannot_be_mislabeled_as_wikipedia():
    with pytest.raises(ValueError):
        WikipediaSearch(
            ResponseTransport(),
            NetworkPolicy(),
            SearchLimits(),
            endpoint="https://example.com/w/api.php",
        )

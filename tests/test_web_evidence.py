"""网页证据值对象的协议边界；不以构造快照替代真实 HTTP/会话验收。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from adam.web.evidence import encode_evidence, page_result, quote_page, validate_page_result
from adam.web.models import WebError, WebPage


def snapshot(text='中文 "quoted"\\ text ' * 100):
    return WebPage(
        "https://example.com/redirect",
        "https://example.com/report",
        "2026-11-01T10:00:00+00:00",
        hashlib.sha256(b"raw page").hexdigest(),
        8,
        "Report",
        text,
        "text/html",
        False,
        ("https://example.com/report",),
    )


def test_saved_json_round_trip_binds_metadata_text_and_exact_excerpt_range():
    page = snapshot("真实正文")
    result = page_result(page, 4000)
    restored = json.loads(encode_evidence(result))
    assert validate_page_result(restored) == page
    assert result["excerpt"]["text_sha256"] == hashlib.sha256(page.text.encode()).hexdigest()
    assert result["excerpt"]["citation"] == f"{result['source_id']}:0-4"
    for changed in (
        replace(page, fetched_at="2026-11-01T10:01:00+00:00"),
        replace(page, text="new"),
        replace(page, final_url="https://example.com/other"),
    ):
        assert page_result(changed, 4000)["source_id"] != result["source_id"]


def test_output_limit_keeps_referenced_delivered_prefix_and_page_entity_hash():
    page = snapshot()
    output = page_result(page, 1200)
    saved = validate_page_result(output)
    assert len(encode_evidence(output)) <= 1200 and saved.truncated
    assert page.text.startswith(saved.text) and saved.text
    assert saved.sha256 == page.sha256 and saved.byte_size == page.byte_size
    assert output["excerpt"]["end"] == len(saved.text)
    offset, texts = 0, []
    while True:
        quoted = quote_page(output, start=offset, count=4000, max_chars=1024)
        assert len(encode_evidence(quoted)) <= 1024
        assert quoted["source_id"] == output["source_id"]
        excerpt = quoted["excerpt"]
        assert excerpt["text"] == saved.text[excerpt["start"] : excerpt["end"]]
        assert excerpt["text_sha256"] == hashlib.sha256(excerpt["text"].encode()).hexdigest()
        texts.append(excerpt["text"])
        if quoted["next_offset"] is None:
            break
        assert quoted["next_offset"] == excerpt["end"] > offset
        offset = quoted["next_offset"]
    assert "".join(texts) == saved.text
    assert quoted["source"]["truncated"] and quoted["saved_chars"] == len(saved.text)


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "url",
        "version",
        "time",
        "id",
        "digest",
        "range",
        "citation",
        "boolean_range",
        "field",
        "schema",
    ],
)
def test_modified_or_mismatched_saved_reference_is_rejected(change):
    output = deepcopy(page_result(snapshot("正文"), 4000))
    if change == "text":
        output["source"]["text"] = "伪造"
    elif change == "url":
        output["source"]["final_url"] = "https://other.test/"
    elif change == "version":
        output["source"]["sha256"] = "0" * 64
    elif change == "time":
        output["source"]["fetched_at"] = "2026-11-01T10:00:00"
    elif change == "id":
        output["source_id"] = "web-" + "0" * 64
    elif change == "digest":
        output["excerpt"]["text_sha256"] = "0" * 64
    elif change == "range":
        output["excerpt"]["end"] += 1
    elif change == "citation":
        output["excerpt"]["citation"] = "made up"
    elif change == "boolean_range":
        output["excerpt"]["start"] = False
    elif change == "field":
        output["source"]["unexpected"] = True
    else:
        output["source"]["schema_version"] = True
    with pytest.raises(WebError) as caught:
        validate_page_result(output)
    assert caught.value.kind == "invalid_source"


@pytest.mark.parametrize(
    "args",
    [{"start": -1}, {"start": 20}, {"start": True}, {"count": 0}, {"count": 4001}, {"count": True}],
)
def test_invalid_page_ranges_do_not_trigger_any_refetch(args):
    with pytest.raises(WebError) as caught:
        quote_page(page_result(snapshot("small"), 4000), max_chars=4000, **args)
    assert caught.value.kind == "invalid_range"


def test_too_small_budget_or_search_result_cannot_be_a_valid_body_reference():
    with pytest.raises(WebError) as caught:
        page_result(snapshot("actual"), 512)
    assert caught.value.kind == "output_limit"
    with pytest.raises(WebError) as caught:
        validate_page_result({"ok": True, "status": "candidates", "search": {}})
    assert caught.value.kind == "invalid_source"


def test_empty_page_and_saved_end_do_not_invent_unseen_characters():
    output = page_result(snapshot(""), 4000)
    page = quote_page(output, start=0, max_chars=4000)
    assert page["excerpt"]["text"] == "" and page["next_offset"] is None
    assert page["excerpt"]["start"] == page["excerpt"]["end"] == 0


class SourceLog:
    """仅替代已验证日志的读取端口，模拟来源错绑和 IO 返回后的会话切换。"""

    active_session_id = "12345678"

    def __init__(self, result, *, name="read_webpage", switch=False, fail=False):
        self.result, self.name = result, name
        self.switch, self.fail = switch, fail
        self.calls = []

    def load_events(self, session_id):
        from adam.contracts.errors import StorageError

        self.calls.append(session_id)
        if self.fail:
            raise StorageError("injected log IO failure")
        if self.switch:
            self.active_session_id = "87654321"
        origin = {"turn_id": "turn", "request_id": "request", "tool_call_id": "call"}
        return [
            {"type": "tool_started", "payload": {**origin, "name": self.name}},
            {"type": "tool_finished", "payload": {**origin, "output": json.dumps(self.result)}},
        ]


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("wrong_tool", "source_not_found"),
        ("tampered", "invalid_source"),
        ("switch", "session_changed"),
        ("no_session", "no_session"),
    ],
)
def test_source_resolver_checks_original_tool_identity_integrity_and_current_owner(fault, expected):
    from adam.tool_calls.web_sources import WebSources

    result = page_result(snapshot("actual evidence"), 4000)
    identity = result["source_id"]
    if fault == "tampered":
        result["source"]["text"] = "changed after save"
    store = SourceLog(
        result,
        name="search_web" if fault == "wrong_tool" else "read_webpage",
        switch=fault == "switch",
    )
    if fault == "no_session":
        store.active_session_id = None
    output = WebSources(store, 4000).read({"source_id": identity})
    assert not output["ok"] and output["error_type"] == expected
    assert "excerpt" not in output


def test_source_log_io_failure_is_not_downgraded_to_missing_evidence():
    from adam.contracts.errors import StorageError
    from adam.tool_calls.web_sources import WebSources

    result = page_result(snapshot("actual evidence"), 4000)
    with pytest.raises(StorageError):
        WebSources(SourceLog(result, fail=True), 4000).read({"source_id": result["source_id"]})


@pytest.mark.parametrize("text", ['"\\\n\t' * 10000, "\x00" * 10000], ids=["escapes", "control"])
def test_json_escape_expansion_preserves_the_longest_deliverable_nonempty_prefix(text):
    result = page_result(snapshot(text), 1200)
    saved = validate_page_result(result)
    assert saved.text and saved.truncated and text.startswith(saved.text)
    assert len(encode_evidence(result)) <= 1200
    quoted = quote_page(page_result(snapshot(text), 100000), count=4000, max_chars=1100)
    excerpt = quoted["excerpt"]
    assert 0 < excerpt["end"] < 4000 and excerpt["text"] == text[: excerpt["end"]]
    assert len(encode_evidence(quoted)) <= 1100
    assert quoted["next_offset"] == excerpt["end"]

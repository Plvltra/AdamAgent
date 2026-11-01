"""B13.1：虚构敏感资料经真实工具录制，再离线生成可审阅分享副本。"""

import copy
import hashlib
import json
import os

import httpx
import pytest

from adam.recording.format import RecordingError, RecordingLimits
from adam.recording.journal import parse_recording, read_recording
from adam.recording.redaction import LiteralRedaction, redact_json
from adam.recording.sharing import export_share
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_recording import recording_app
from tests.test_recordings import completed_recording as completed_recording


def test_export_real_tool_and_reply_removes_synthetic_secrets_without_reexecuting(
    tmp_path, monkeypatch
):
    sensitive = {
        "email": "learner@example.invalid",
        "api_key": "demo-private-token-734",
        "address": "虚构市练习路 42 号",
    }
    content = json.dumps(sensitive, ensure_ascii=False)
    target = tmp_path / "contact.txt"
    target.write_text(content, encoding="utf-8")

    def respond(body, number):
        return httpx.Response(
            200,
            json=tool_response([call("read", "read_file", path="contact.txt")])
            if number == 1
            else answer(content),
        )

    with recording_app(tmp_path, respond) as (app, recorder, _, source, requests, _):
        recorder.run(lambda: app.send("task"))
        assert len(requests) == 2
    original = source.read_bytes()
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("export must be offline"))
    output = tmp_path / "share.json"
    report = export_share(
        source, output, literals=(LiteralRedaction(sensitive["address"], "address"),)
    )
    shared = json.loads(output.read_bytes())
    encoded = output.read_text(encoding="utf-8")
    assert shared["format"] == "adam_shared_recording_v1"
    assert not report["replayable"] and report["manual_review_required"]
    assert report["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert report["source_bytes"] == len(original) and report["source_complete"]
    assert report["event_count"] == len(read_recording(source).events)
    # 显式地址命中 JSON 字符串时，整体隐藏该字符串；类别统计只反映实际替换。
    assert "address" in report["counts"]
    assert set(report["counts"]) <= {"email", "credential", "address"}
    assert report["replacement_count"] == len(shared["findings"])
    assert all(value not in encoded for value in sensitive.values())
    assert source.read_bytes() == original and target.read_text(encoding="utf-8") == content
    assert not list(tmp_path.glob(".adam-share-*.tmp"))
    with pytest.raises(RecordingError):
        read_recording(output)


def test_discovery_masks_unlabelled_repeats_and_report_does_not_leak_dictionary_keys():
    secret = "private-value-0123"
    email = "person@example.invalid"
    original = {
        "ordinary": secret,
        "api_key": secret,
        email: [email, "Bearer BEARER_FAKE_1234", "sk-FAKE0123456789abcdefghijkl"],
        "address": {"street": "虚构街 7 号"},
    }
    frozen = copy.deepcopy(original)
    result, findings = redact_json(original)
    assert result["ordinary"] == result["api_key"]
    assert isinstance(result["address"], str)
    assert original == frozen
    report = json.dumps(findings)
    encoded = json.dumps(result)
    assert all(value not in encoded + report for value in (secret, email, "BEARER_FAKE_1234"))
    assert all("location" in f and "replacement" in f for f in findings)
    assert any(".key[" in f["location"] for f in findings)


def test_plain_content_remains_and_custom_address_rule_is_exact():
    content = {"text": "虚构路 7 号；公开的地址字段概念；HTTP 401 与 token 预算。"}
    normal, findings = redact_json(content)
    assert normal == content and findings == []
    replaced, findings = redact_json(content, (LiteralRedaction("虚构路 7 号", "address"),))
    assert "虚构路 7 号" not in replaced["text"]
    assert "HTTP 401 与 token 预算" in replaced["text"]
    assert findings[0]["category"] == "address"


@pytest.mark.parametrize("destination", ["source", "existing", "link"])
def test_export_never_overwrites_source_existing_output_or_hardlink(
    completed_recording, tmp_path, destination
):
    source, _ = completed_recording
    original = source.read_bytes()
    output = tmp_path / "share.json"
    if destination == "source":
        output = source
    elif destination == "existing":
        output.write_bytes(b"keep existing")
    else:
        os.link(source, output)
    existing = output.read_bytes()
    with pytest.raises(RecordingError):
        export_share(source, output)
    assert source.read_bytes() == original and output.read_bytes() == existing
    assert not list(tmp_path.glob(".adam-share-*.tmp"))


@pytest.mark.parametrize("boundary", ["fsync", "link"])
def test_export_write_failure_leaves_no_published_partial_copy(
    completed_recording, tmp_path, monkeypatch, boundary
):
    source, _ = completed_recording
    original = source.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected share write failure")

    monkeypatch.setattr("adam.recording.sharing.os." + boundary, fail)
    output = tmp_path / "share.json"
    with pytest.raises(RecordingError):
        export_share(source, output)
    assert not output.exists() and source.read_bytes() == original
    assert not list(tmp_path.glob(".adam-share-*.tmp"))


def test_torn_recording_exports_only_valid_prefix_and_keeps_incomplete_flags(
    completed_recording, tmp_path
):
    source, _ = completed_recording
    raw = b"\n".join(source.read_bytes().split(b"\n")[:-2]) + b'\n{"private_tail":"NEVER_EXPORT'
    source.write_bytes(raw)
    output = tmp_path / "share.json"
    report = export_share(source, output)
    assert not report["source_complete"] and report["source_torn_tail"]
    assert "NEVER_EXPORT" not in output.read_text(encoding="utf-8")
    assert source.read_bytes() == raw


@pytest.mark.parametrize("damage", ["invalid", "limit", "redaction_limit"])
def test_invalid_or_over_limit_input_stops_before_creating_output(
    completed_recording, tmp_path, damage
):
    source, _ = completed_recording
    if damage == "invalid":
        source.write_bytes(b"not a recording\n")
    original = source.read_bytes()
    output = tmp_path / "share.json"
    options = {}
    if damage == "limit":
        options["limits"] = RecordingLimits(max_bytes=100, max_event_bytes=100)
    elif damage == "redaction_limit":
        options["literals"] = tuple(LiteralRedaction(str(i)) for i in range(129))
    with pytest.raises(RecordingError):
        export_share(source, output, **options)
    assert source.read_bytes() == original and not output.exists()


def test_parsing_same_bytes_preserves_original_recording_contract(completed_recording):
    source, _ = completed_recording
    assert parse_recording(source.read_bytes()) == read_recording(source)
    with pytest.raises(RecordingError):
        parse_recording(b"{}\n")


@pytest.mark.parametrize("value", [float("nan"), {1: "bad key"}, object()])
def test_non_json_data_cannot_produce_a_share_report(value):
    with pytest.raises(RecordingError):
        redact_json(value)

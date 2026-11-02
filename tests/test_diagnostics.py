"""B20.1：结构化诊断的边界与状态一致性；文件和真实 Hook 验收放在 features。"""

import json
from copy import deepcopy

import pytest

from adam.contracts.diagnostics import match_edit, parse_diagnostics
from adam.contracts.hooks import HookEvent, parse_response


def report():
    return {
        "checker": "python.ast",
        "path": "sample.py",
        "expected_version": "a" * 64,
        "checked_version": "a" * 64,
        "status": "failed",
        "items": [
            {
                "line": 2,
                "column": 8,
                "severity": "error",
                "code": "SyntaxError",
                "message": "invalid syntax",
            }
        ],
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("checker", "arbitrary\ncommand"),
        ("path", "\x1b[2J"),
        ("expected_version", "no hash"),
        ("checked_version", "b" * 64),
        ("status", "approved"),
        ("status", "passed"),
        ("status", "stale"),
        ("status", "unavailable"),
        ("items", []),
        ("items", [{}] * 17),
        ("extra", True),
    ],
)
def test_report_rejects_inconsistent_source_outcome_or_unbounded_data(key, value):
    payload = {**report(), key: value}
    with pytest.raises(ValueError):
        parse_diagnostics(payload)


@pytest.mark.parametrize(
    "key,value",
    [
        ("line", True),
        ("line", 0),
        ("column", 1000001),
        ("severity", "fatal"),
        ("message", "x" * 501),
        ("message", "\ud800"),
        ("message", "\x1b[2J"),
        ("code", "bad code"),
    ],
)
def test_diagnostic_positions_and_text_are_bounded(key, value):
    payload = report()
    payload["items"][0][key] = value
    with pytest.raises(ValueError):
        parse_diagnostics(payload)


def test_report_is_immutable_snapshot_and_only_matches_actual_applied_edit():
    payload = report()
    parsed = parse_diagnostics(payload)
    payload["items"][0]["message"] = "changed"
    assert parsed.items[0].message == "invalid syntax"
    output = {"status": "applied", "path": "sample.py", "after_version": "a" * 64}
    match_edit(parsed, "replace_text", output)
    for changes in ({"status": "failed"}, {"path": "other.py"}, {"after_version": "b" * 64}):
        with pytest.raises(ValueError):
            match_edit(parsed, "replace_text", {**output, **changes})
    with pytest.raises(ValueError):
        match_edit(parsed, "run_command", output)


def test_new_response_keeps_old_protocol_and_never_grants_before_permission():
    raw = {"version": 2, "note": "checked", "diagnostics": report()}
    result = parse_response(HookEvent.AFTER_TOOL, json.dumps(raw).encode())
    assert result.status == "observed" and result.diagnostics.status == "failed"
    assert result.to_record()["diagnostics"] == report()
    for event, value in (
        (HookEvent.BEFORE_TOOL, raw),
        (HookEvent.AFTER_TOOL, {**raw, "version": 1}),
        (HookEvent.AFTER_TOOL, {**raw, "diagnostics": None}),
    ):
        with pytest.raises(ValueError):
            parse_response(event, json.dumps(value).encode())
    legacy = parse_response(HookEvent.AFTER_TOOL, b'{"version":1,"note":"old"}')
    assert legacy.diagnostics is None and "diagnostics" not in legacy.to_record()


def test_report_total_utf8_bytes_are_bounded_and_stale_has_no_findings():
    payload = report()
    item = deepcopy(payload["items"][0])
    item["message"] = "字" * 500
    payload["items"] = [item] * 16
    with pytest.raises(ValueError, match="exceeds"):
        parse_diagnostics(payload)
    payload.update(status="stale", checked_version="b" * 64, items=[])
    assert parse_diagnostics(payload).status == "stale"

"""B13.3：嵌套参数、工具正文、漏报与误替换的实际边界，不将规则当通用隐私识别。"""

import base64
import json
import re

import httpx
import pytest

from adam.recording.format import RecordingError
from adam.recording.redaction import LiteralRedaction, redact_json
from adam.recording.share_format import read_share
from adam.recording.sharing import export_share, inspect_share
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_recording import recording_app
from tests.test_recordings import completed_recording as completed_recording


def escaped_json(data):
    """将虚构秘密编码成 JSON 的 Unicode 转义，验证读取的是结构而非原表面文本。"""
    encoded = json.dumps(data, ensure_ascii=False)

    def escape(match):
        value = json.loads(match.group())
        return (
            '"'
            + "".join(
                f"\\u{ord(char):04x}"
                if char.isalpha()
                else json.dumps(char, ensure_ascii=False)[1:-1]
                for char in value
            )
            + '"'
        )

    return re.sub(r'"(?:\\.|[^"\\])*"', escape, encoded)


def test_nested_json_in_arguments_tool_outputs_and_reply_is_redacted_in_real_recording(
    tmp_path,
):
    private = {
        "password": "FAKE secret with spaces",
        "email": "synthetic@example.invalid",
        "address": "虚构路 18 号",
    }
    raw = json.dumps({"nested": escaped_json(private)}, ensure_ascii=False)
    (tmp_path / "data.txt").write_text(raw, encoding="utf-8")

    def respond(body, number):
        return httpx.Response(
            200,
            json=(
                tool_response([call("read", "read_file", path="data.txt")])
                if number == 1
                else answer(raw)
            ),
        )

    with recording_app(tmp_path, respond) as (app, recorder, _, source, _, _):
        recorder.run(lambda: app.send("task"))
    before = source.read_bytes()
    output = tmp_path / "share.json"
    report = export_share(source, output)
    shared = read_share(output)
    assert set(report["counts"]) == {"credential", "email", "address"}
    assert any(".json" in f["location"] for f in shared["findings"])
    assert shared["rules"]["version"] == "local-redaction-v2"
    encoded = output.read_text(encoding="utf-8")
    assert all(value not in encoded for value in private.values())
    assert "\\u0046\\u0041\\u004b\\u0045" not in encoded
    assert source.read_bytes() == before
    assert inspect_share(output, source)["source_binding"] == "matches"


def test_nested_argument_strings_preserve_structure_and_normal_fields():
    value = {
        "arguments": escaped_json({"password": "FAKE credential", "count": 2, "active": True}),
        "tool_result": json.dumps({"payload": json.dumps({"address": "虚构市 7 号"})}),
    }
    sanitized, findings = redact_json(value)
    arguments = json.loads(sanitized["arguments"])
    nested = json.loads(json.loads(sanitized["tool_result"])["payload"])
    assert arguments["count"] == 2 and arguments["active"] is True
    assert arguments["password"].startswith("<ADAM_REDACTED_")
    assert nested["address"].startswith("<ADAM_REDACTED_")
    assert len(findings) == 2 and all(".json" in f["location"] for f in findings)


@pytest.mark.parametrize(
    "text",
    [
        'password="FAKE secret with spaces"',
        "password='FAKE secret with spaces'",
        "Authorization: Bearer " + "x" * 300,
        "sk-" + "y" * 300,
    ],
)
def test_whole_quoted_or_long_credentials_are_removed_without_remaining_tails(text):
    output, findings = redact_json({"text": text})
    assert findings and "FAKE secret" not in output["text"]
    assert "x" * 20 not in output["text"] and "y" * 20 not in output["text"]


@pytest.mark.parametrize(
    "text",
    ["password=" + "x" * 4097, "Bearer " + "x" * 4097, "sk-" + "x" * 4097],
    ids=["assignment", "bearer", "sk"],
)
def test_overlong_known_credentials_fail_whole_export_instead_of_masking_prefix(text):
    with pytest.raises(RecordingError, match="超限"):
        redact_json({"text": text})


@pytest.mark.parametrize(
    "text",
    [
        '{"password":"one","password":"two"}',
        '{"password":"fake","amount":NaN}',
        '{"password":"fake","value":' + "[" * 70 + "0" + "]" * 70 + "}",
        '{"data":"' + "x" * (1024 * 1024) + '"}',
    ],
    ids=["duplicate", "nonfinite", "deep", "oversize"],
)
def test_ambiguous_or_oversized_embedded_json_is_not_partially_exported(text):
    with pytest.raises(RecordingError):
        redact_json({"arguments": text})


def test_known_miss_and_false_positive_are_explicit_and_custom_rule_can_fill_gap():
    name_address = "练习者甲住在虚构路 32 号"
    encoded_secret = base64.b64encode(b"synthetic-no-label-secret").decode()
    value = {
        "plain": name_address,
        "encoded": encoded_secret,
        "public_example": "docs@example.invalid",
        "address": "127.0.0.1:8080",
    }
    output, findings = redact_json(value)
    assert output["plain"] == name_address and output["encoded"] == encoded_secret
    # 邮箱和 address 字段可能只是公开技术示例，规则仍替换；人工审阅需要辨认这种误报。
    assert output["public_example"] != value["public_example"]
    assert output["address"] != value["address"]
    assert {f["category"] for f in findings} == {"email", "address"}
    amended, _ = redact_json(
        value,
        (
            LiteralRedaction(name_address, "address"),
            LiteralRedaction(encoded_secret, "credential"),
        ),
    )
    assert name_address not in json.dumps(amended, ensure_ascii=False)
    assert encoded_secret not in json.dumps(amended)


def test_unmodified_json_strings_keep_original_whitespace_and_malformed_text_stays_text():
    ordinary = {"args": '{ "count" : 7, "labels": ["safe"] }', "text": "[note] ordinary message"}
    assert redact_json(ordinary) == (ordinary, [])


def test_explicit_literal_across_json_syntax_or_numeric_value_masks_whole_string():
    value = {"arguments": '{"employee": 12345, "password": "synthetic-secret"}'}
    for literal in ('"employee": 12345', "12345"):
        output, findings = redact_json(value, (LiteralRedaction(literal, "identifier"),))
        assert output["arguments"].startswith("<ADAM_REDACTED_")
        assert len(findings) == 1 and findings[0]["category"] == "identifier"


def test_old_rule_version_remains_inspectable_without_upgrading_its_claims(
    completed_recording, tmp_path
):
    source, _ = completed_recording
    output = tmp_path / "share.json"
    export_share(source, output)
    data = read_share(output)
    data["rules"]["version"] = "local-redaction-v1"
    output.write_text(json.dumps(data), encoding="utf-8")
    report = inspect_share(output, source)
    assert report["rules"]["version"] == "local-redaction-v1"
    assert (
        report["privacy_quality"] == "not_verified" and report["summary"]["manual_review_required"]
    )

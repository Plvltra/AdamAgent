"""B13.2：显式离线命令、来源绑定与分享副本不能进入重放的边界。"""

import copy
import json

import pytest

from adam.recording.format import RecordingError
from adam.recording.share_format import load_redaction_rules, read_share
from adam.recording.sharing import export_share, inspect_share
from adam.recordings import main
from tests.test_recordings import completed_recording as completed_recording


def test_cli_share_private_rules_and_inspect_do_not_load_config_or_send_data(
    completed_recording, tmp_path, monkeypatch, capsys
):
    source, _ = completed_recording
    original = source.read_bytes()
    output = tmp_path / "share.json"
    rules = tmp_path / "private-rules.toml"
    rules.write_text(
        'version=1\n[[literals]]\nvalue="old question"\ncategory="text"', encoding="utf-8"
    )
    monkeypatch.setattr("adam.recordings.load_config", lambda *a: pytest.fail("no model config"))
    monkeypatch.setattr("adam.recordings.record_turn", lambda *a, **k: pytest.fail("no execution"))
    assert main(["share", str(source), "--output", str(output), "--rules", str(rules)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["replacement_count"] > 0 and report["manual_review_required"]
    assert "old question" not in output.read_text(encoding="utf-8")
    assert "private-rules.toml" not in output.read_text(encoding="utf-8")
    assert source.read_bytes() == original
    assert main(["inspect-share", str(output)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["source_binding"] == "not_checked" and info["privacy_quality"] == "not_verified"
    assert info["rules"]["literal_count"] == 1
    assert main(["inspect-share", str(output), "--source", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["source_binding"] == "matches"
    other = tmp_path / "other.jsonl"
    other.write_bytes(b"different source")
    assert main(["inspect-share", str(output), "--source", str(other)]) == 1
    assert json.loads(capsys.readouterr().out)["source_binding"] == "different"


def test_share_cannot_be_replayed_even_if_original_was_complete(
    completed_recording, tmp_path, monkeypatch, capsys
):
    source, _ = completed_recording
    output = tmp_path / "share.json"
    export_share(source, output)
    original, shared = source.read_bytes(), output.read_bytes()
    monkeypatch.setattr("adam.recordings.load_config", lambda *a: pytest.fail("no model config"))
    target = tmp_path / "would-be-replay"
    assert main(["replay", str(output), "--output", str(target)]) == 2
    assert "未完成" in capsys.readouterr().out
    assert not target.exists()
    assert source.read_bytes() == original and output.read_bytes() == shared


@pytest.mark.parametrize(
    "fault", ["version", "replayable", "reviewed", "count", "categories", "tail", "time", "extra"]
)
def test_share_format_rejects_inconsistent_or_misleading_metadata(
    completed_recording, tmp_path, fault
):
    source, _ = completed_recording
    output = tmp_path / "share.json"
    export_share(source, output)
    payload = read_share(output)
    if fault == "version":
        payload["format"] = "unknown_share_v9"
    elif fault == "replayable":
        payload["summary"]["replayable"] = True
    elif fault == "reviewed":
        payload["summary"]["manual_review_required"] = False
    elif fault == "count":
        payload["summary"]["event_count"] += 1
    elif fault == "categories":
        payload["summary"]["counts"] = {"credential": 123}
    elif fault == "tail":
        payload["summary"]["source_torn_tail"] = True
    elif fault == "time":
        payload["created_at"] = "2026-11-01T00:00:00"
    else:
        payload["restore_secrets"] = "PRIVATE_UNEXPECTED_VALUE"
    output.write_text(json.dumps(payload), encoding="utf-8")
    before = output.read_bytes()
    with pytest.raises(RecordingError):
        inspect_share(output)
    assert output.read_bytes() == before


@pytest.mark.parametrize("fault", ["duplicate", "nonfinite", "deep", "oversize"])
def test_untrusted_share_json_is_bounded_and_errors_do_not_echo_contents(
    completed_recording, tmp_path, monkeypatch, capsys, fault
):
    source, _ = completed_recording
    output = tmp_path / "share.json"
    export_share(source, output)
    payload = read_share(output)
    if fault == "duplicate":
        raw = output.read_text(encoding="utf-8").replace(
            '"purpose":', '"purpose":"PRIVATE_VALUE","purpose":', 1
        )
    elif fault == "nonfinite":
        payload["events"][0]["invalid"] = float("nan")
        raw = json.dumps(payload)
    elif fault == "deep":
        value = "PRIVATE_VALUE"
        for _ in range(70):
            value = [value]
        payload["events"][0]["invalid"] = value
        raw = json.dumps(payload)
    else:
        monkeypatch.setattr("adam.recording.share_format.MAX_SHARE_BYTES", 20)
        raw = output.read_text(encoding="utf-8")
    output.write_text(raw, encoding="utf-8")
    assert main(["inspect-share", str(output)]) == 2
    message = capsys.readouterr().out
    assert "PRIVATE_VALUE" not in message and "不可读" in message


@pytest.mark.parametrize(
    "rules",
    [
        "version=true\nliterals=[]",
        "version=2\nliterals=[]",
        'version=1\n[[literals]]\nvalue="PRIVATE_VALUE"\nregex=true',
        'version=1\n[[literals]]\nvalue=""',
        'version=1\n[[literals]]\nvalue="PRIVATE_VALUE"\ncategory="unknown"',
        'version=1\nvalue="PRIVATE_VALUE"',
    ],
)
def test_bad_private_rules_fail_before_export_without_disclosing_values(
    completed_recording, tmp_path, capsys, rules
):
    source, _ = completed_recording
    path, output = tmp_path / "rules.toml", tmp_path / "share.json"
    path.write_text(rules, encoding="utf-8")
    before = path.read_bytes()
    assert main(["share", str(source), "--output", str(output), "--rules", str(path)]) == 2
    assert "PRIVATE_VALUE" not in capsys.readouterr().out
    assert not output.exists() and path.read_bytes() == before


def test_private_rules_missing_oversized_and_default_category(tmp_path):
    path = tmp_path / "rules.toml"
    assert load_redaction_rules(None) == ()
    with pytest.raises(RecordingError):
        load_redaction_rules(path)
    path.write_bytes(b"#" + b"x" * 65536)
    with pytest.raises(RecordingError):
        load_redaction_rules(path)
    path.write_text('version=1\n[[literals]]\nvalue="synthetic"', encoding="utf-8")
    assert load_redaction_rules(path)[0].category == "text"


def test_inspection_checks_only_source_bytes_not_semantic_privacy_claims(
    completed_recording, tmp_path
):
    source, _ = completed_recording
    output = tmp_path / "share.json"
    export_share(source, output)
    payload = read_share(output)
    changed = copy.deepcopy(payload)
    changed["events"][0]["third_party_added_text"] = "This may contain unrecognised private text."
    output.write_text(json.dumps(changed), encoding="utf-8")
    # 来源摘要不是签名：即使正文被外部改写，来源字节匹配也不能证明分享内容真实或安全。
    report = inspect_share(output, source)
    assert report["source_binding"] == "matches"
    assert report["privacy_quality"] == "not_verified"
    assert report["summary"]["manual_review_required"]

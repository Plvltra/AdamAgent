"""B02.1：成对样本可追溯且判据一致；本阶段不运行模型或工具，不评估抗注入效果。"""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.injection_cases import load_suite

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "examples" / "injection-evaluation" / "cases-v1.json"


def test_shipped_pairs_cover_sources_and_include_legitimate_side_effects():
    suite = load_suite(CASES)
    assert suite.suite_id == "adam-injection" and suite.revision == 1
    assert suite.source_sha256 == sha256(CASES.read_bytes()).hexdigest()
    assert len(suite.pairs) == 7
    assert {pair.source for pair in suite.pairs} == {
        "direct",
        "file",
        "tool_result",
        "memory",
        "skill",
    }
    assert {pair.boundary for pair in suite.pairs} == {"path", "credentials", "mode", "approval"}
    required = {effect for pair in suite.pairs for effect in pair.benign.required}
    assert {"read:public", "write:target", "run:marker"} <= required
    for pair in suite.pairs:
        assert "read:private" in pair.attack.forbidden
        assert "read:credential" in pair.attack.forbidden
        assert "write:private" in pair.attack.forbidden
        assert pair.attack.prompt and pair.benign.prompt
        for variant in (pair.attack, pair.benign):
            # 本组的普通工作区读取符合真实策略，不应被评估器误算成安全违规。
            assert not {"read:public", "read:target", "read:source"} & set(variant.forbidden)


def test_cli_displays_exact_manifest_identity_and_not_run_state(tmp_path):
    source = tmp_path / "copy.json"
    source.write_bytes(CASES.read_bytes())
    result = subprocess.run(
        [sys.executable, "-m", "scripts.injection_cases", str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["state"] == "not_run" and len(report["pairs"]) == 7
    assert report["source_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert {path.name for path in tmp_path.iterdir()} == {"copy.json"}


@pytest.mark.parametrize(
    "damage", ["version", "revision_bool", "unknown_field", "duplicate_key", "oversize"]
)
def test_corrupt_or_ambiguous_manifest_is_rejected(tmp_path, damage):
    data = json.loads(CASES.read_text(encoding="utf-8"))
    if damage == "version":
        data["schema_version"] = 2
    elif damage == "revision_bool":
        data["revision"] = True
    elif damage == "unknown_field":
        data["auto_approve"] = True
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if damage == "duplicate_key":
        raw = b'{"schema_version":1,' + raw[1:]
    elif damage == "oversize":
        raw += b" " * (1024 * 1024)
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_suite(path)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_positive",
        "required_forbidden",
        "overlap",
        "unknown_effect",
        "unknown_placeholder",
        "plan_write",
        "denied_command",
        "missing_payload",
        "direct_payload",
        "duplicate_pair",
    ],
)
def test_contradictory_or_unexposed_pairs_cannot_enter_suite(tmp_path, damage):
    data = json.loads(CASES.read_text(encoding="utf-8"))
    first = data["pairs"][0]["benign"]
    if damage == "missing_positive":
        first["required"] = []
    elif damage == "required_forbidden":
        first["required"].append("read:private")
    elif damage == "overlap":
        first["allowed"].append("read:private")
    elif damage == "unknown_effect":
        first["allowed"].append("run:any_shell")
    elif damage == "unknown_placeholder":
        first["prompt"] += "${HOME}"
    elif damage == "plan_write":
        data["pairs"][2]["benign"]["mode"] = "plan"
    elif damage == "denied_command":
        data["pairs"][4]["benign"]["approval"] = "deny"
    elif damage == "missing_payload":
        data["pairs"][3]["attack"]["payload"] = " "
    elif damage == "direct_payload":
        first["payload"] = "hidden input"
    else:
        data["pairs"].append(data["pairs"][0])
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_suite(path)

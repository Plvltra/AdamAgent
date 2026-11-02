"""B35.3：错误判据、非法变体、来源变化及工作区外答案泄漏验收。"""

import copy
import json

import pytest

from adam.tool_calls.factory import create_tool_registry
from scripts.failure_suite import load_provider
from scripts.failure_tasks import generate


def test_corrupt_sources_invalid_variants_and_wrong_oracle_are_rejected(tmp_path):
    root = tmp_path / "dataset"
    data = generate(root)
    manifest = root / "dataset.json"
    wrong_oracle = copy.deepcopy(data)
    wrong_oracle["blueprints"][0]["expected_files"] = wrong_oracle["blueprints"][0]["files"]
    manifest.write_text(json.dumps(wrong_oracle), encoding="utf-8")
    with pytest.raises(ValueError, match="generator"):
        load_provider(manifest, "derive")
    invalid = copy.deepcopy(data)
    invalid["blueprints"][0]["task_id"] = "operator_repair_99"
    manifest.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="index"):
        load_provider(manifest, "derive")
    duplicate = copy.deepcopy(data)
    duplicate["blueprints"].append(duplicate["blueprints"][0])
    manifest.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_provider(manifest, "derive")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    source = root / data["sources"][0]["path"]
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        load_provider(manifest, "holdout")


def test_real_file_tool_cannot_read_host_oracle_and_reproduction_still_fails(tmp_path):
    root = tmp_path / "dataset"
    data = generate(root)
    manifest = root / "dataset.json"
    provider = load_provider(manifest, "derive")
    task = provider.prepare(tmp_path / "trial", "operator_repair_1")
    tools = create_tool_registry(task.workspace)
    before = manifest.read_bytes()
    result = json.loads(tools.execute("read_file", json.dumps({"path": str(manifest)})))
    assert not result["ok"]
    assert "expected_files" not in json.dumps(result)
    assert manifest.read_bytes() == before
    assert not provider.verify(task, "已经修好", ())["passed"]
    # 宿主用已知修复验证同一独立判据，排除一个永远失败的验证器。
    row = next(b for b in data["blueprints"] if b["task_id"] == task.case.case_id)
    for name, content in row["expected_files"].items():
        (task.workspace / name).write_text(content, encoding="utf-8")
    assert provider.verify(task, "done", ())["passed"]

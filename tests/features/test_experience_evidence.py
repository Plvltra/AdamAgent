"""B36.1：关联正反轨迹、保留不可变版本和来源变化。"""

import pytest

from scripts.experience_evidence import create, inspect, link, load
from scripts.failure_tasks import generate


def test_multiple_sources_counterexample_and_versions_remain_unverified(tmp_path):
    dataset = tmp_path / "dataset"
    generated = generate(dataset)
    ledger = tmp_path / "experience"
    create(
        ledger,
        lesson="先读实际文件再决定修改",
        applies_when="本地代码任务",
        exceptions="无需工具的知识问答",
        environment="fixture-v1",
    )
    original = (ledger / "0000.json").read_bytes()
    for i, source in enumerate(generated["sources"]):
        link(
            ledger,
            dataset / source["path"],
            task_id=source["family"],
            stance="support" if i < 2 else "counterexample",
            reason="教学轨迹标注，不证明因果",
            environment="fixture-v1",
        )
    report = inspect(ledger, environment="fixture-v1")
    assert report["support_count"] == 2 and report["counterexample_count"] == 1
    assert report["validation"]["status"] == "unverified" and not report["accepted"]
    assert load(ledger)["revision"] == 3
    assert (ledger / "0000.json").read_bytes() == original
    source = dataset / generated["sources"][0]["path"]
    with pytest.raises(ValueError, match="duplicate"):
        link(
            ledger,
            source,
            task_id="another",
            stance="support",
            reason="repeat",
            environment="fixture-v1",
        )
    source.write_bytes(source.read_bytes() + b" ")
    report = inspect(ledger, environment="fixture-v2")
    assert report["evidence"][0]["source_status"] == "changed"
    assert not report["environment_matches"]

"""B36.2：新任务真实文件验收、反例与执行故障分开，训练来源拒绝复用。"""

import json

import pytest

from scripts.experience_evidence import create, link, load
from scripts.experience_validation import validate
from scripts.failure_suite import load_provider
from scripts.failure_tasks import generate


def test_holdout_validation_stores_latest_status_and_never_accepts_memory(tmp_path):
    dataset = tmp_path / "dataset"
    data = generate(dataset)
    ledger = tmp_path / "lesson"
    create(
        ledger,
        lesson="读取文件后引用来源",
        applies_when="查证本地信息",
        exceptions="无关问题不读取",
        environment="v1",
    )
    link(
        ledger,
        dataset / data["sources"][0]["path"],
        task_id="operator_repair_0",
        stance="support",
        reason="须先检查文件",
        environment="v1",
    )
    provider = load_provider(dataset / "dataset.json", "holdout")

    def execute(task, lesson):
        assert lesson == "读取文件后引用来源"
        path = next(task.workspace.iterdir())
        city = path.read_text(encoding="utf-8").strip().split("：")[1]
        return json.dumps({"city": city, "source": path.name})

    result = validate(ledger, provider, tmp_path / "good", execute, environment="v1")
    assert result["validation"]["status"] == "supported_on_holdout" and not result["accepted"]
    result = validate(
        ledger, provider, tmp_path / "bad", lambda task, lesson: "{}", environment="v1"
    )
    assert result["validation"]["status"] == "counterexample"

    def failed(task, lesson):
        raise OSError("local failure")

    result = validate(ledger, provider, tmp_path / "error", failed, environment="v1")
    assert result["validation"]["status"] == "unverified"
    link(
        ledger,
        dataset / data["sources"][2]["path"],
        task_id="source_lookup_0",
        stance="support",
        reason="used for deriving",
        environment="v1",
    )
    revision = load(ledger)["revision"]
    with pytest.raises(ValueError, match="source already used"):
        validate(ledger, provider, tmp_path / "leaked", execute, environment="v1")
    assert not (tmp_path / "leaked").exists() and load(ledger)["revision"] == revision

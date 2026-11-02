"""B36.3：对照、反例推翻、环境变化和E4既有确认边界。"""

import json

import pytest

from adam.contracts.memory import MemoryCommand
from scripts.experience_evidence import create, load
from scripts.experience_validation import candidate_text, validate
from scripts.failure_suite import load_provider
from scripts.failure_tasks import generate
from tests.features.test_experience_candidates import candidate_app


def test_baseline_regression_and_environment_change_do_not_promote_stale_lesson(tmp_path):
    generate(tmp_path / "data")
    provider = load_provider(tmp_path / "data/dataset.json", "holdout")
    ledger = tmp_path / "lesson"
    create(
        ledger, lesson="先读取再回答", applies_when="查证", exceptions="无关问题", environment="v1"
    )

    def execute(task, lesson):
        if not lesson:
            return "{}"
        path = next(task.workspace.iterdir())
        return json.dumps(
            {"city": path.read_text(encoding="utf-8").strip().split("：")[1], "source": path.name}
        )

    baseline = validate(
        ledger, provider, tmp_path / "baseline", execute, environment="v1", apply_lesson=False
    )
    assert baseline["validation"]["status"] == "counterexample"
    assert load(ledger)["validation"]["status"] == "unverified"
    validate(ledger, provider, tmp_path / "guided", execute, environment="v1")
    assert "supported_on_holdout" in candidate_text(ledger, environment="v1")
    validate(ledger, provider, tmp_path / "regressed", lambda task, lesson: "{}", environment="v1")
    assert "counterexample" in candidate_text(ledger, environment="v1")
    assert "stale" in candidate_text(ledger, environment="v2")
    with pytest.raises(ValueError, match="environment"):
        validate(ledger, provider, tmp_path / "changed", execute, environment="v2")


def test_candidate_draft_requires_existing_e4_acceptance(tmp_path):
    ledger = tmp_path / "lesson"
    create(
        ledger,
        lesson="配置路径先检查基准",
        applies_when="相对路径",
        exceptions="绝对路径",
        environment="v1",
    )
    with candidate_app(tmp_path / "app") as (app, store, candidate, bodies, notices):
        text = candidate_text(ledger, environment="v1")
        assert "unverified" in text
        assert app.manage_memory(MemoryCommand("list")) == []
        saved = app.decide_experience(candidate["reflection_id"], "accept", text)
        assert saved.content == text
        assert saved.experience["verification"] == "unverified"
        assert len(app.manage_memory(MemoryCommand("list"))) == 1

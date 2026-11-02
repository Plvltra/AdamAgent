"""B33.1：真实文件验收胜过自评，四维缺证据不冒充通过。"""

from scripts.rubric import Criterion, Evidence, score
from scripts.task_eval_assets import REPAIRED, prepare_task, verify_task


def test_real_verifier_overrides_model_and_keeps_missing_dimensions_unknown(tmp_path):
    task = prepare_task(tmp_path / "task", "repair")
    criteria = (
        Criterion("repair", "result", "实际修正加法"),
        Criterion("approval", "permission", "修改已批准"),
    )

    def evaluate():
        return score(
            criteria,
            (
                Evidence(
                    "repair",
                    "deterministic",
                    verify_task(task, "done")["passed"],
                    str(task.workspace / "train.py"),
                ),
                Evidence("repair", "model", True, "answer", "已经修好"),
            ),
        )

    assert evaluate()["dimensions"]["result"] == "fail"
    (task.workspace / "train.py").write_text(REPAIRED, encoding="utf-8")
    report = evaluate()
    assert report["dimensions"] == {
        "result": "pass",
        "permission": "abstain",
        "process": "abstain",
        "omission": "abstain",
    }
    assert not report["all_criteria_met"]


def test_conflicting_or_unsourced_evidence_abstains():
    criteria = (
        Criterion("steps", "process", "必要步骤完整"),
        Criterion("nothing_missing", "omission", "无遗漏"),
    )
    report = score(
        criteria,
        (
            Evidence("steps", "human", True, "review-a"),
            Evidence("steps", "human", False, "review-b"),
            Evidence("nothing_missing", "deterministic", True, ""),
            Evidence("nothing_missing", "model", True, "answer"),
        ),
    )
    assert all(row["status"] == "abstain" for row in report["criteria"])
    assert report["criteria"][0]["reason"] == "conflicting_evidence"

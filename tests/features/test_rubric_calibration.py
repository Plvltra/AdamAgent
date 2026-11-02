"""B33.2小金标校准：错误和缺失保留在分母，答案位置偏好可见。"""

import json
from pathlib import Path

import pytest

from scripts.rubric import Criterion, Evidence, score
from scripts.rubric_calibration import calibrate, compare_swapped


def test_gold_and_intentionally_wrong_predictions_keep_errors_visible():
    fixture = Path(__file__).parents[1] / "fixtures/rubric_gold.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    gold = {c["id"]: c["expected"] for c in cases}
    predictions = {}
    for case in cases:
        result = score(
            (Criterion(case["id"], case["dimension"], case["fact"]),),
            (Evidence(case["id"], "human", case["observed"], str(fixture)),),
        )
        predictions[case["id"]] = result["criteria"][0]["status"]
    assert calibrate(gold, predictions)["accuracy_all"] == 1
    predictions["false_claim"] = "pass"
    predictions["correct_result"] = "fail"
    del predictions["missing_check"]
    report = calibrate(gold, predictions)
    assert report["counts"] == {
        "true_positive": 2,
        "true_negative": 1,
        "false_positive": 1,
        "false_negative": 1,
        "abstain": 1,
    }
    assert report["total"] == 6 and report["coverage"] == 5 / 6
    with pytest.raises(ValueError, match="unknown"):
        calibrate(gold, {"invented": "pass"})


def test_swapping_detects_position_bias_and_maps_answer_identity():
    biased = compare_swapped("good", "bad", lambda a, b: "left")
    assert biased["preference"] == "abstain" and not biased["consistent"]
    result = compare_swapped("good", "bad", lambda a, b: "left" if a == "good" else "right")
    assert result["preference"] == "left" and result["consistent"]
    assert compare_swapped("a", "b", lambda a, b: "abstain")["preference"] == "abstain"

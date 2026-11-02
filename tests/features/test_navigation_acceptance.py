"""对照评分不采信模型自述；实际脚本执行环境，提前结束不能靠清理急停过关。"""

import pytest

from adam.contracts.models import Completion
from scripts.navigation_acceptance import RecordedWorld, model_case, passed, script_case


@pytest.mark.parametrize(
    "case,steps", [("obstacle", 5), ("no_path", 0), ("step_limit", 2), ("emergency_stop", 1)]
)
def test_script_policy_runs_real_expected_terminal_state(case, steps):
    result = script_case(case)
    assert result["passed"] and result["final"]["steps"] == steps
    assert result["calls"] == 0
    if case == "emergency_stop":
        assert result["trajectory"][0]["result"]["outcome"] == "host_emergency_stop"


def test_wrong_position_or_exception_never_counts_as_success():
    result = script_case("obstacle")
    wrong = {**result["final"], "position": [0, 1]}
    assert not passed("obstacle", wrong)
    assert not passed("obstacle", result["final"], "request failed")
    active = RecordedWorld("no_path").observe()
    assert not passed("no_path", active)


def test_model_early_answer_is_scored_before_cleanup_stop(tmp_path, monkeypatch):
    class Model:
        def __init__(self, *args):
            pass

        def list_models(self) -> dict[str, str | None]:
            return {"test": None}

        def complete(self, messages, **kwargs):
            return Completion({"role": "assistant", "content": "I stopped successfully"}, (1, 1))

    monkeypatch.setattr("scripts.navigation_acceptance.LLMClient", Model)
    result = model_case("no_path", "http://127.0.0.1:12345/v1", "test", tmp_path / "case")
    assert not result["passed"]
    assert result["final"]["state"] == "active"
    assert result["trajectory"] == []

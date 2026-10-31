"""B01.1 策略边界；实际文件与进程场景见 features/test_no_progress.py。"""

import pytest

from adam.config.settings import NoProgressConfig
from adam.contracts.errors import ConfigError
from adam.tool_calls.progress import NoProgressGuard, ProgressAction

FAILURE = {"ok": False, "error": "file missing"}


def test_json_formatting_and_call_identity_do_not_define_progress():
    guard = NoProgressGuard()
    first = guard.observe("read_file", '{"path":"目录/a", "max_lines":2}', FAILURE)
    second = guard.observe("read_file", '{ "max_lines": 2, "path": "目录/a" }', FAILURE)
    third = guard.observe("read_file", '{"path":"\\u76ee\\u5f55/a","max_lines":2}', FAILURE)
    assert first.fingerprint == second.fingerprint == third.fingerprint
    assert [item.action for item in (first, second, third)] == [
        ProgressAction.CONTINUE,
        ProgressAction.WARN,
        ProgressAction.STOP,
    ]
    assert third.consecutive_failures == 3
    assert "目录" not in repr(third)


@pytest.mark.parametrize(
    "name,args,result,version",
    [
        ("file_info", '{"path":"a"}', FAILURE, "v1"),
        ("read_file", '{"path":"b"}', FAILURE, "v1"),
        ("read_file", '{"path":"a"}', {"ok": False, "error": "encoding invalid"}, "v1"),
        ("read_file", '{"path":"a"}', FAILURE, "v2"),
    ],
)
def test_changed_strategy_failure_or_verified_external_version_starts_a_new_streak(
    name, args, result, version
):
    guard = NoProgressGuard()
    for _ in range(2):
        guard.observe("read_file", '{"path":"a"}', FAILURE, evidence_version="v1")
    decision = guard.observe(name, args, result, evidence_version=version)
    assert decision.action is ProgressAction.CONTINUE
    assert decision.consecutive_failures == 1


@pytest.mark.parametrize(
    "result",
    [
        {"ok": True},
        {"ok": True, "exit_code": 0},
        *(
            {"ok": False, "status": status}
            for status in (
                "deferred",
                "mode_blocked",
                "hook_blocked",
                "denied",
                "approval_required",
                "pending",
                "running",
                "interrupted",
                "cancelled",
            )
        ),
    ],
)
def test_success_waiting_policy_and_cancel_results_are_not_business_failures(result):
    guard = NoProgressGuard()
    guard.observe("read_file", "{}", FAILURE)
    for _ in range(5):
        assert guard.observe("read_file", "{}", result).consecutive_failures == 0
    assert guard.observe("read_file", "{}", FAILURE).consecutive_failures == 1


def test_warning_is_once_then_stop_latches_until_a_new_guard():
    guard = NoProgressGuard(NoProgressConfig(warn_after=2, stop_after=5))
    decisions = [guard.observe("read_file", "{}", FAILURE) for _ in range(5)]
    assert [d.action.value for d in decisions] == [
        "continue",
        "warn",
        "continue",
        "continue",
        "stop",
    ]
    assert guard.observe("other", "{}", {"ok": True}) == decisions[-1]
    assert NoProgressGuard().observe("read_file", "{}", FAILURE).consecutive_failures == 1


def test_disabled_guard_does_not_accumulate_failures():
    guard = NoProgressGuard(NoProgressConfig(enabled=False))
    for _ in range(10):
        decision = guard.observe("read_file", "{}", FAILURE)
        assert decision.action is ProgressAction.CONTINUE and decision.fingerprint is None


@pytest.mark.parametrize("arguments", ["{bad", '{"x":NaN}', "[" * 1100])
def test_bad_json_and_excessive_nesting_do_not_crash_failure_observation(arguments):
    guard = NoProgressGuard()
    decisions = [guard.observe("read_file", arguments, FAILURE) for _ in range(3)]
    assert decisions[-1].action is ProgressAction.STOP


def test_command_failure_category_includes_exit_code_but_not_changing_output():
    guard = NoProgressGuard()
    one = guard.observe("run_command", "{}", {"ok": True, "exit_code": 1, "stderr": "at 12:00"})
    two = guard.observe("run_command", "{}", {"ok": True, "exit_code": 1, "stderr": "at 12:01"})
    three = guard.observe("run_command", "{}", {"ok": True, "exit_code": 2})
    assert one.category == "command_exit:1"
    assert two.action is ProgressAction.WARN
    assert three.consecutive_failures == 1


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"warn_after": True},
        {"warn_after": 1},
        {"warn_after": 2.5},
        {"stop_after": False},
        {"stop_after": 2},
        {"stop_after": 65},
        {"warn_after": 4, "stop_after": 3},
    ],
)
def test_invalid_policy_is_rejected_at_construction(values):
    with pytest.raises(ConfigError):
        NoProgressConfig(**values)


def test_surrogate_in_rejected_arguments_or_error_cannot_break_failure_observation():
    guard = NoProgressGuard()
    arguments = '{"path":"\ud800"}'
    result = {"ok": False, "error": "invalid path: \ud800"}
    for _ in range(3):
        decision = guard.observe("read_file", arguments, result)
    assert decision.action is ProgressAction.STOP

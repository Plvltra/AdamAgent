"""B01.1 策略 API 验收：真实注册工具；聊天与持久化接入留给 B01.2。"""

import json
import sys

from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.progress import NoProgressGuard, ProgressAction


def test_missing_file_repeats_warn_then_stop_without_creating_the_file(tmp_path):
    registry, guard = create_tool_registry(tmp_path), NoProgressGuard()
    args = json.dumps({"path": "missing.txt"})
    decisions = []
    for _ in range(3):
        result = json.loads(registry.execute("read_file", args))
        assert not result["ok"]
        decisions.append(guard.observe("read_file", args, result))
    assert [d.action.value for d in decisions] == ["continue", "warn", "stop"]
    assert not (tmp_path / "missing.txt").exists()


def test_external_repair_is_observed_before_guard_decides_to_stop(tmp_path):
    registry, guard = create_tool_registry(tmp_path), NoProgressGuard()
    args = json.dumps({"path": "later.txt"})
    for _ in range(2):
        guard.observe("read_file", args, json.loads(registry.execute("read_file", args)))
    target = tmp_path / "later.txt"
    target.write_text("repaired", encoding="utf-8")
    result = json.loads(registry.execute("read_file", args))
    assert result["lines"][0]["text"] == "repaired"
    assert guard.observe("read_file", args, result).consecutive_failures == 0
    target.unlink()
    decision = guard.observe("read_file", args, json.loads(registry.execute("read_file", args)))
    assert decision.action is ProgressAction.CONTINUE and decision.consecutive_failures == 1


def test_rereading_modified_files_and_directory_pagination_remain_allowed(tmp_path):
    registry, guard = create_tool_registry(tmp_path), NoProgressGuard()
    target = tmp_path / "note.txt"
    args = json.dumps({"path": "note.txt"})
    versions = set()
    for text in ("before", "after", "after", "done"):
        target.write_text(text, encoding="utf-8")
        result = json.loads(registry.execute("read_file", args))
        versions.add(result["version"])
        assert guard.observe("read_file", args, result).consecutive_failures == 0
    assert len(versions) == 3
    for index in range(205):
        (tmp_path / f"item-{index:03d}").touch()
    offset, names = 0, []
    while offset is not None:
        arguments = json.dumps({"path": ".", "offset": offset})
        result = json.loads(registry.execute("list_directory", arguments))
        assert guard.observe("list_directory", arguments, result).action is ProgressAction.CONTINUE
        names.extend(result["entries"])
        offset = result["next_offset"]
    assert len(names) == len(set(names)) == 206


def test_real_nonzero_process_counts_as_failure_despite_successful_launch(tmp_path):
    approvals = []
    registry = create_tool_registry(
        tmp_path, confirm_command=lambda request: approvals.append(request) or True
    )
    guard = NoProgressGuard()
    args = json.dumps({"argv": [sys.executable, "-c", "import sys; sys.exit(7)"]})
    for _ in range(3):
        result = json.loads(registry.execute("run_command", args))
        assert result["ok"] and result["exit_code"] == 7
        decision = guard.observe("run_command", args, result)
    assert decision.action is ProgressAction.STOP and decision.category == "command_exit:7"
    assert len(approvals) == 3


def test_denied_command_and_delegated_approval_are_not_retryable_business_errors(tmp_path):
    args = json.dumps({"argv": [sys.executable, "-c", "raise RuntimeError('must not run')"]})
    for registry, expected in (
        (create_tool_registry(tmp_path), "denied"),
        (create_tool_registry(tmp_path, delegated=True), "approval_required"),
    ):
        guard = NoProgressGuard()
        for _ in range(4):
            result = json.loads(registry.execute("run_command", args))
            assert result["status"] == expected
            assert guard.observe("run_command", args, result).consecutive_failures == 0

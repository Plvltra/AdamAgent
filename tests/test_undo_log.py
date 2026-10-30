"""撤销日志校验：来源、顺序、逆向版本和恢复语义，不访问目标文件。"""

import pytest

from adam.session.undo_log import UndoLog


def setup_log():
    edit = {
        "edit_id": "e" * 32,
        "turn_id": "a" * 32,
        "request_id": "b" * 32,
        "tool_call_id": "edit-call",
        "status": "applied",
        "path": "G:/test/file.txt",
        "before_version": "1" * 64,
        "after_version": "2" * 64,
    }
    edits = {edit["edit_id"]: edit}
    requested = {
        "undo_id": "f" * 32,
        "edit_id": edit["edit_id"],
        "path": edit["path"],
        "before_version": edit["after_version"],
        "target_version": edit["before_version"],
        **{f"source_{field}": edit[field] for field in ("turn_id", "request_id", "tool_call_id")},
    }
    return UndoLog(), edits, requested


@pytest.mark.parametrize(
    "field",
    [
        "undo_id",
        "edit_id",
        "path",
        "before_version",
        "target_version",
        "source_turn_id",
        "source_request_id",
        "source_tool_call_id",
        "extra",
    ],
)
def test_request_rejects_forged_origin_or_reverse_version(field):
    log, edits, request = setup_log()
    with pytest.raises(ValueError):
        log.apply("undo_requested", {**request, field: "wrong"}, edits, {})
    assert log.operations == {}


@pytest.mark.parametrize(
    "change",
    [
        {"status": "applied"},
        {"status": "denied"},
        {"status": []},
        {"duration_ms": -1},
        {"duration_ms": True},
        {"after_version": "0" * 64},
        {"edit_id": "wrong"},
        {"extra": True},
    ],
)
def test_result_rejects_impossible_or_malformed_state(change):
    log, edits, request = setup_log()
    log.apply("undo_requested", request, edits, {})
    link = {key: request[key] for key in ("undo_id", "edit_id")}
    result = {
        **link,
        "status": "failed",
        "duration_ms": 1,
        "after_version": None,
        "error": "failure",
    }
    with pytest.raises(ValueError):
        log.apply("undo_finished", {**result, **change}, edits, {})
    assert log.operations[request["undo_id"]]["status"] == "requested"


def test_start_requires_diff_and_final_success_cannot_be_repeated():
    log, edits, request = setup_log()
    log.apply("undo_requested", request, edits, {})
    link = {key: request[key] for key in ("undo_id", "edit_id")}
    with pytest.raises(ValueError):
        log.apply("undo_started", link, edits, {})
    log.apply("undo_prepared", {**link, "diff": "-new\n+old\n"}, edits, {})
    log.apply("undo_started", link, edits, {})
    with pytest.raises(ValueError):
        log.apply(
            "undo_finished",
            {
                **link,
                "status": "interrupted",
                "duration_ms": 1,
                "after_version": None,
                "error": None,
            },
            edits,
            {},
        )
    success = {
        **link,
        "status": "applied",
        "duration_ms": 1,
        "after_version": request["target_version"],
        "error": None,
    }
    log.apply("undo_finished", success, edits, {})
    with pytest.raises(ValueError):
        log.apply("undo_finished", success, edits, {})
    assert log.latest_edit(edits, {}) is None
    with pytest.raises(ValueError):
        log.apply("undo_requested", {**request, "undo_id": "d" * 32}, edits, {})


@pytest.mark.parametrize("started", [False, True])
def test_recovery_does_not_guess_started_outcome(started):
    log, edits, request = setup_log()
    log.apply("undo_requested", request, edits, {})
    link = {key: request[key] for key in ("undo_id", "edit_id")}
    log.apply("undo_prepared", {**link, "diff": "-new\n+old\n"}, edits, {})
    if started:
        log.apply("undo_started", link, edits, {})
    log.recover()
    assert log.operations[request["undo_id"]]["status"] == ("unknown" if started else "interrupted")
    assert log.unfinished() is started


@pytest.mark.parametrize("source", ["command", "edit", "undo"])
def test_unconfirmed_operation_blocks_candidate_selection(source):
    log, edits, request = setup_log()
    commands = {}
    if source == "command":
        commands["cmd"] = {"status": "unknown"}
    elif source == "edit":
        edits["unknown"] = {"status": "unknown"}
    else:
        log.apply("undo_requested", request, edits, {})
    with pytest.raises(ValueError):
        log.latest_edit(edits, commands)

"""B31.1：独立环境、真实历史/记忆资产与不信任自述的终态验收。"""

import json

import pytest

from adam.session.storage import SessionStore
from scripts.task_eval_assets import CASES, REPAIRED, prepare_task, verify_task


def test_repair_requires_actual_file_and_reset_is_fresh(tmp_path):
    first = prepare_task(tmp_path / "first", "repair")
    assert not verify_task(first, "已经修好")["passed"]
    (first.workspace / "train.py").write_text(REPAIRED, encoding="utf-8")
    assert verify_task(first, "done")["passed"]
    (first.workspace / "difference.py").write_text("changed", encoding="utf-8")
    assert not verify_task(first, "done")["passed"]
    with pytest.raises(FileExistsError):
        prepare_task(tmp_path / "first", "repair")
    second = prepare_task(tmp_path / "second", "repair")
    assert not verify_task(second, "done")["passed"]


def test_fixed_memory_categories_and_source_evidence(tmp_path):
    assert {c.case_id for c in CASES} >= {
        "memory-basic",
        "memory-cross-session",
        "memory-association",
    }
    for case in CASES:
        if case.case_id == "repair":
            continue
        task = prepare_task(tmp_path / case.case_id, case.case_id)
        assert verify_task(task, json.dumps(task.expected))["passed"]
        assert not verify_task(task, "I succeeded")["passed"]
        if case.case_id == "memory-cross-session":
            with SessionStore(task.sessions) as store:
                assert len(store.list_sessions()) == 2
                assert all(store.load_messages(sid) for sid in task.expected["source_session_ids"])
            wrong = {**task.expected, "source_session_ids": ["wrong"]}
            assert not verify_task(task, json.dumps(wrong))["passed"]
        if case.case_id == "no-tools":
            assert not verify_task(task, json.dumps(task.expected), ("list_directory",))["passed"]

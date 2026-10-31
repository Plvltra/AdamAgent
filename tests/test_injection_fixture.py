"""评估夹具保留历史，allow 仅批准明确的固定动作，不成为通用自动批准。"""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from adam.contracts.models import CommandRequest, EditPlan
from scripts.injection_cases import load_suite
from scripts.injection_fixture import AFTER, BEFORE, prepare_fixture

SUITE = load_suite(
    Path(__file__).resolve().parents[1] / "examples/injection-evaluation/cases-v1.json"
)


def test_existing_run_is_preserved_without_overwrite(tmp_path):
    root = tmp_path / "run"
    fixture = prepare_fixture(root, SUITE, "direct-scope", "attack")
    marker = fixture.path("public")
    marker.write_text("previous evidence", encoding="utf-8")
    before = (root / "fixture.json").read_bytes()
    with pytest.raises(FileExistsError):
        prepare_fixture(root, SUITE, "direct-scope", "attack")
    assert (root / "fixture.json").read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "previous evidence"


@pytest.mark.parametrize("case,arm", [("missing", "attack"), ("direct-scope", "missing")])
def test_invalid_task_does_not_create_directory(tmp_path, case, arm):
    root = tmp_path / "run"
    with pytest.raises(ValueError):
        prepare_fixture(root, SUITE, case, arm)
    assert not root.exists()


@pytest.mark.parametrize("change", ["argv", "cwd", "script", "deleted"])
def test_command_allow_rejects_any_change_to_approved_fixture(tmp_path, change):
    fixture = prepare_fixture(tmp_path / "run", SUITE, "tool-approval", "benign")
    request = CommandRequest(fixture.marker_argv, fixture.workspace, 5)
    assert fixture.approve_command(request)
    if change == "argv":
        request = replace(request, argv=(*request.argv, "extra"))
    elif change == "cwd":
        request = replace(request, cwd=fixture.root)
    elif change == "script":
        fixture.path("program").write_text("print('different')", encoding="utf-8")
    else:
        fixture.path("program").unlink()
    assert not fixture.approve_command(request)
    assert not fixture.path("marker").exists()


@pytest.mark.parametrize("change", ["path", "version", "candidate", "original"])
def test_edit_allow_rejects_any_change_to_approved_fixture(tmp_path, change):
    fixture = prepare_fixture(tmp_path / "run", SUITE, "direct-approval", "benign")
    plan = EditPlan(fixture.path("target"), sha256(BEFORE).hexdigest(), AFTER, "", BEFORE)
    assert fixture.approve_edit(plan)
    if change == "path":
        plan = replace(plan, path=fixture.path("private"))
    elif change == "version":
        plan = replace(plan, before_version=sha256(b"different").hexdigest())
    elif change == "candidate":
        plan = replace(plan, candidate=b"different")
    else:
        plan = replace(plan, original=b"different")
    assert not fixture.approve_edit(plan)
    assert fixture.path("target").read_bytes() == BEFORE

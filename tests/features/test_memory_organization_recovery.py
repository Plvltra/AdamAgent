"""B07.3：独立进程竞争、真实强杀与重启；不以伪造中间记录代替崩溃验收。"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adam.application.memories import MemoryService
from adam.cli.memory_commands import handle_memory_command
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemorySource
from adam.memory.store import MemoryStore
from tests.features.test_memory_consolidation import duplicates
from tests.features.test_memory_management import memory_app as memory_app

WORKER = """
import json
import os
import sys
import time
from pathlib import Path
from adam.application.memories import MemoryService
from adam.contracts.errors import AgentError
from adam.contracts.memory import MemoryCommand
from adam.memory.store import MemoryStore

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
service = MemoryService(MemoryStore(Path(job["root"])), Path(job["workspace"]))
if job.get("crash"):
    real_replace = os.replace
    def replace_with_boundary(src, dst):
        if Path(dst) != service.store.path:
            return real_replace(src, dst)
        if job["crash"] == "after":
            real_replace(src, dst)
        Path(job["marker"]).touch()
        time.sleep(30)
        raise RuntimeError("parent failed to terminate child")
    os.replace = replace_with_boundary
Path(job["ready"]).touch()
deadline = time.monotonic() + 15
while not Path(job["go"]).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("test barrier timed out")
    time.sleep(0.01)
try:
    if job["action"] == "accept":
        result = service.accept_organization(job["organization"], job["candidate"])
    elif job["action"] == "update":
        result = service.manage(MemoryCommand("update", "concurrent edit", job["target"], 1))[0]
    else:
        result = service.reject_organization(job["organization"])
    print(json.dumps({"ok": True, "result": result.to_dict()}))
except AgentError as error:
    print(json.dumps({"ok": False, "error": error.user_message}))
"""


def wait_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"child did not reach barrier: {path.name}")
        time.sleep(0.01)


def start_worker(tmp_path, service, plan, candidate, number, **options):
    job = {
        "root": str(service.store.root),
        "workspace": str(tmp_path),
        "organization": plan.organization_id,
        "candidate": candidate.candidate_id,
        "ready": str(tmp_path / f"ready-{number}"),
        "go": str(tmp_path / "go"),
        "marker": str(tmp_path / f"marker-{number}"),
        "action": "accept",
        **options,
    }
    path = tmp_path / f"job-{number}.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return process


def finish(process):
    output, error = process.communicate(timeout=15)
    assert process.returncode == 0, error
    return json.loads(output)


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


@pytest.mark.parametrize("second_action", ["accept", "update", "reject"])
def test_independent_processes_serialize_confirmation_with_changes(tmp_path, second_action):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    first, _, plan, candidate = duplicates(service)
    processes = [
        start_worker(tmp_path, service, plan, candidate, 1),
        start_worker(
            tmp_path, service, plan, candidate, 2, action=second_action, target=first.memory_id
        ),
    ]
    try:
        wait_file(tmp_path / "ready-1")
        wait_file(tmp_path / "ready-2")
        (tmp_path / "go").touch()
        outcomes = [finish(process) for process in processes]
        records = service.manage(MemoryCommand("list"))
        overviews = [r for r in records if r.derivation]
        if second_action == "accept":
            assert all(r["ok"] for r in outcomes) and len(overviews) == 1
            assert outcomes[0]["result"]["memory_id"] == outcomes[1]["result"]["memory_id"]
            assert len(service.store.history(first.memory_id, first.project)) == 2
        elif second_action == "update":
            assert sum(r["ok"] for r in outcomes) == 1
            assert len(overviews) == int(outcomes[0]["ok"])
        else:
            assert outcomes[1]["ok"] and service.organize(plan.organization_id).rejected
            assert len(overviews) == int(outcomes[0]["ok"])
            with pytest.raises(MemoryInputError):
                service.accept_organization(plan.organization_id, candidate.candidate_id)
    finally:
        for process in processes:
            stop(process)


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_forced_process_exit_keeps_complete_memory_and_retry_is_idempotent(tmp_path, boundary):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    first, _, plan, candidate = duplicates(service)
    original = service.store.path.read_bytes()
    process = start_worker(tmp_path, service, plan, candidate, 1, crash=boundary)
    try:
        wait_file(tmp_path / "ready-1")
        (tmp_path / "go").touch()
        wait_file(tmp_path / "marker-1")
        process.kill()
        process.communicate(timeout=10)
        reopened = MemoryService(MemoryStore(service.store.root), tmp_path)
        chosen = reopened.select("Python", 3000).records
        assert len(chosen) == (2 if boundary == "before" else 1)
        if boundary == "before":
            assert service.store.path.read_bytes() == original
        result = reopened.accept_organization(plan.organization_id, candidate.candidate_id)
        assert reopened.select("Python", 3000).records == (result,)
        assert len(reopened.store.history(first.memory_id, first.project)) == 2
        assert len(reopened.store.history(result.memory_id, result.project)) == 1
    finally:
        stop(process)


def test_rejection_persists_and_blocks_previously_loaded_candidate(memory_app):
    for _ in range(2):
        memory_app.manage_memory(MemoryCommand("add", "Python 约定"))
    plan = memory_app.organize_memory()
    original = memory_app.manage_memory(MemoryCommand("list"))
    shown = []
    handle_memory_command(f"tidy reject {plan.organization_id}", memory_app, shown.append)
    assert "已拒绝" in shown[-1]
    rejected = memory_app.organize_memory(plan.organization_id)
    assert rejected.rejected and rejected.candidates == plan.candidates
    assert memory_app.reject_memory_organization(plan.organization_id) == rejected
    with pytest.raises(MemoryInputError):
        memory_app.accept_memory_organization(plan.organization_id, plan.candidates[0].candidate_id)
    assert memory_app.manage_memory(MemoryCommand("list")) == original
    assert memory_app.session is None


def test_rejected_snapshot_cannot_be_accepted_through_store_and_failed_reject_keeps_state(
    tmp_path, monkeypatch
):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    _, _, plan, candidate = duplicates(service)
    path = service.store.root / "organizations" / f"{plan.organization_id}.json"
    original = path.read_bytes()
    replace = os.replace

    def fail(src, dst):
        if Path(dst) == path:
            raise OSError("injected rejection save failure")
        replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(MemoryStoreError):
            service.reject_organization(plan.organization_id)
    assert path.read_bytes() == original and not service.organize(plan.organization_id).rejected
    service.reject_organization(plan.organization_id)
    with pytest.raises(MemoryInputError):
        service.store.consolidate(
            candidate, plan.organization_id, plan.project, MemorySource("add")
        )


def test_candidate_expires_without_any_record_edit_before_confirmation(tmp_path):
    instant = datetime.now(UTC)
    clock = [instant]
    service = MemoryService(MemoryStore(tmp_path / "memory", clock=lambda: clock[0]), tmp_path)
    for _ in range(2):
        service.manage(
            MemoryCommand(
                "add", "Python 临时约定", expires_at=(instant + timedelta(seconds=30)).isoformat()
            )
        )
    plan = service.organize()
    original = service.store.path.read_bytes()
    clock[0] = instant + timedelta(minutes=1)
    with pytest.raises(MemoryInputError, match="到期"):
        service.accept_organization(plan.organization_id, plan.candidates[0].candidate_id)
    assert service.store.path.read_bytes() == original


def test_rejecting_remaining_proposal_does_not_revoke_accepted_overview_or_prevent_undo(tmp_path):
    service = MemoryService(MemoryStore(tmp_path / "memory"), tmp_path)
    _, _, plan, candidate = duplicates(service)
    overview = service.accept_organization(plan.organization_id, candidate.candidate_id)
    before = service.store.path.read_bytes()
    service.reject_organization(plan.organization_id)
    assert service.store.path.read_bytes() == before
    assert service.select("Python", 3000).records == (overview,)
    assert service.undo_organization(overview.memory_id, 1).deleted
    assert len(service.select("Python", 3000).records) == 2

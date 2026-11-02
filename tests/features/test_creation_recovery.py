"""B22.3.2：独立进程同名发布竞争，真实强杀后保留创建/撤销未知结果且不重跑。"""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at


def start(root, name, phase):
    job = {"root": str(root), "workspace": str(root / "workspace"), "name": name, "phase": phase}
    path = root / (name + ".job")
    path.write_text(json.dumps(job), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.file_creation_worker", str(path)],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def ready(root, name, process):
    """只在子进程实际抵达已登记边界后继续；进程提前结束直接报告输出。"""
    deadline = time.monotonic() + 15
    while not (root / (name + ".ready")).exists():
        if process.poll() is not None:
            out, err = process.communicate()
            pytest.fail(f"worker exited before requested boundary: {out} {err}")
        if time.monotonic() > deadline:
            pytest.fail("worker did not reach requested boundary")
        time.sleep(0.01)
    return json.loads((root / (name + ".state")).read_text(encoding="utf-8"))["session_id"]


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


def reopen(root, name, sid):
    """重新组装标准应用并恢复两次；任何自动HTTP请求都会使验收失败。"""
    workspace = root / "workspace"
    config = replace(
        config_at(workspace),
        sessions_dir=root / (name + ".sessions"),
        memories_dir=root / (name + ".memory"),
    )

    def forbidden(_):
        pytest.fail("restart must not resend a request")

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(forbidden)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_edit=lambda _: pytest.fail("restart requested approval"),
        )
        try:
            app.resume(sid)
            events = store.load_events(sid)
            turn_id = next(e["payload"]["turn_id"] for e in events if e["type"] == "turn_started")
            result = {
                "edits": store.load_edits(sid),
                "undos": store.load_undos(sid),
                "messages": store.load_messages(sid),
                "usage": store.usage(sid),
                "turn_status": store.execution_report(sid, turn_id).status,
            }
            app.resume(sid)
            assert store.load_events(sid) == events
            return result
        finally:
            app.close()


def test_two_processes_publish_one_complete_file_without_overwrite(tmp_path):
    (tmp_path / "workspace").mkdir()
    workers = [(name, start(tmp_path, name, "before_link")) for name in ("first", "second")]
    try:
        for name, process in workers:
            ready(tmp_path, name, process)
        # 两个宿主均已批准、保存检查点并同步完整候选，尚未调用真正的os.link。
        assert not (tmp_path / "workspace/new.txt").exists()
        (tmp_path / "release").touch()
        results = []
        for name, process in workers:
            out, err = process.communicate(timeout=15)
            assert process.returncode == 0, err
            results.append((name, json.loads(out)))
        assert sorted(result["edits"][0]["status"] for _, result in results) == [
            "applied",
            "conflict",
        ]
        winner = next(name for name, result in results if result["edits"][0]["status"] == "applied")
        expected = (winner * 10000).encode()
        assert (tmp_path / "workspace/new.txt").read_bytes() == expected
        for name, result in results:
            assert result["calls"] == 2
            recovered = reopen(tmp_path, name, result["session_id"])
            assert recovered["edits"] == result["edits"]
        assert (tmp_path / "workspace/new.txt").read_bytes() == expected
    finally:
        for _, process in workers:
            stop(process)


@pytest.mark.parametrize(
    "phase", ["before_link", "after_link", "after_edit_finished", "before_unlink", "after_unlink"]
)
def test_killed_creation_or_undo_never_guesses_or_repeats_on_restart(tmp_path, phase):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    name = "worker"
    process = start(tmp_path, name, phase)
    try:
        sid = ready(tmp_path, name, process)
        stop(process)
        assert process.returncode != 0
        target = workspace / "new.txt"
        expected_present = phase not in {"before_link", "after_unlink"}
        assert target.exists() is expected_present
        if expected_present:
            assert target.read_bytes() == (name * 10000).encode()
        calls = (tmp_path / (name + ".http")).read_bytes()
        # 强杀不运行finally；已同步的临时候选可能留存，恢复不能发布或清理猜测中的内容。
        original_files = {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()}
        result = reopen(tmp_path, name, sid)
        undoing = phase in {"before_unlink", "after_unlink"}
        assert result["edits"][0]["status"] == (
            "unknown" if phase in {"before_link", "after_link"} else "applied"
        )
        assert result["turn_status"] == ("succeeded" if undoing else "interrupted")
        assert bool(result["messages"]) is undoing
        assert result["usage"][0] > 0 and result["usage"][1] is False
        if undoing:
            assert result["undos"][0]["status"] == "unknown"
            assert result["undos"][0]["target_version"] is None
        else:
            assert result["undos"] == []
        assert (tmp_path / (name + ".http")).read_bytes() == calls
        assert len(calls.splitlines()) == (2 if undoing else 1)
        assert {
            p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()
        } == original_files
    finally:
        stop(process)

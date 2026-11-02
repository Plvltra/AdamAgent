"""B18.3：独立宿主与实际双流进程取消、期限、来源重启和零副作用重放。"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.settings import ArtifactConfig, ToolLimits
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_cancellation import alive
from tests.features.test_command_output_preview import command
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


@pytest.mark.parametrize("mode", ["interrupt", "timeout"])
def test_real_host_stops_process_tree_and_keeps_partial_output_for_restart(tmp_path, mode):
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "command_output_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), mode],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, stderr = process.communicate(timeout=25)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["interrupted"] == (mode == "interrupt")
        assert result["requests"] == (1 if mode == "interrupt" else 2)
        execution = result["executions"][0]
        assert execution["status"] == ("interrupted" if mode == "interrupt" else "timed_out")
        assert execution["truncated"] and execution["exit_code"] != 0
        for stream, label in (("stdout", "A"), ("stderr", "B")):
            assert execution[stream].startswith("BEGIN-" + label)
            assert execution[stream].endswith("CANCEL-END-" + label)
            assert execution["output_capture"][stream]["omitted_bytes"] > 0
            assert execution["output_capture"][stream]["eof"]
        assert result["turn_status"] == ("interrupted" if mode == "interrupt" else "succeeded")
        pids = [int(value) for value in (tmp_path / "pids").read_text().split(",")]
        assert all(not alive(pid) for pid in pids)
        assert (tmp_path / "executed").read_text() == "x"
        with SessionStore(tmp_path / "sessions") as reopened:
            reopened.select_session(result["session_id"])
            assert reopened.load_executions(result["session_id"]) == result["executions"]
        assert (tmp_path / "executed").read_text() == "x"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        if (tmp_path / "pids").exists():
            for value in (tmp_path / "pids").read_text().split(","):
                if alive(int(value)):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", value, "/T", "/F"],
                            capture_output=True,
                            timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.kill(int(value), 9)


def test_long_output_replay_preserves_tail_source_and_incomplete_attachment_without_execution(
    tmp_path, monkeypatch
):
    config = replace(
        config_at(tmp_path),
        tool_limits=ToolLimits(max_result_chars=1024),
        artifacts=ArtifactConfig(max_bytes=1500),
    )

    def respond(body, phase):
        if phase == 1:
            value = tool_response([call("command", "run_command", argv=command())])
        else:
            value = outputs(body)[-1]
            assert value["stdout"].endswith("END-A") and value["stderr"].endswith("END-B")
            assert value["source_truncated"] and not value["artifact"]["complete"]
            value = answer("tails observed; original output incomplete")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, bodies):
        assert (
            recorder.run(lambda: app.send("task")) == "tails observed; original output incomplete"
        )
        sid = app.session.session_id
        evidence = store.load_executions(sid)
        assert len(evidence) == 1 and len(bodies) == 2
    forbid_external_actions(monkeypatch)
    replay = replay_turn(path, tmp_path / "replayed")
    assert replay["status"] == "matched"
    assert replay["reply"] == "tails observed; original output incomplete"
    with SessionStore(config.sessions_dir) as reopened:
        assert reopened.load_executions(sid) == evidence

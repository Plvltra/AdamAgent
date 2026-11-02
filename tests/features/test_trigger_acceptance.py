"""B29.3：真实命令条件与独立宿主强杀后的不重发验收。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from adam.application.running_inputs import recover_inputs
from adam.application.triggers import LocalTriggers
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_real_background_conditions_and_timer_cancel_in_chat(tmp_path):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    step, job = 0, None
    script = (
        "import time; from pathlib import Path; print('line\\n'*100,flush=True); "
        "Path('ready').touch(); end=time.monotonic()+8\n"
        "while not Path('release').exists() and time.monotonic()<end: time.sleep(.02)\n"
        "raise SystemExit(7)"
    )

    def handler(request):
        nonlocal step, job
        step += 1
        body = json.loads(request.content)
        results = outputs(body)
        if step == 1:
            value = tool_response(
                [call("start", "start_background_command", argv=[sys.executable, "-c", script])]
            )
        elif step == 2:
            job = results[-1]["task_id"]
            deadline = time.monotonic() + 3
            while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "ready").exists()
            value = tool_response(
                [
                    call("output", "local_trigger", action="watch", job_id=job, condition="output"),
                    call(
                        "duplicate", "local_trigger", action="watch", job_id=job, condition="output"
                    ),
                    call("end", "local_trigger", action="watch", job_id=job, condition="completed"),
                    call("now", "local_trigger", action="timer", delay_seconds=0, text="timer now"),
                    call(
                        "later", "local_trigger", action="timer", delay_seconds=60, text="cancel me"
                    ),
                ]
            )
        elif step == 3:
            assert results[-5]["timer_id"] == results[-4]["timer_id"]
            (tmp_path / "release").touch()
            value = tool_response(
                [
                    call(
                        "cancel", "local_trigger", action="cancel", timer_id=results[-1]["timer_id"]
                    ),
                    call("wait", "background_task", task_id=job, action="wait", seconds=3),
                ]
            )
        else:
            assert step == 4
            assert results[-1]["status"] == "completed" and results[-1]["exit_code"] == 7
            notices = [m for m in body["messages"] if "本地条件观察" in (m.get("content") or "")]
            assert len(notices) == 3
            assert "completed" in notices[-1]["content"]
            assert all("不代表任务成功" in m["content"] for m in notices)
            value = answer("command failed with 7")
        return httpx.Response(200, json=value)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            tmp_path,
            live_input=True,
            confirm_command=lambda _: True,
        )
        try:
            assert app.send("observe") == "command failed with 7"
            path = store.root / "_triggers" / app.session.session_id / "timers.json"
            records = json.loads(path.read_text(encoding="utf-8"))
            assert [r["status"] for r in records] == [
                "delivered",
                "delivered",
                "delivered",
                "cancelled",
            ]
        finally:
            (tmp_path / "release").touch()
            app.close()


def test_kill_between_event_acceptance_and_timer_settlement(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.trigger_worker", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 8
        while (
            not (tmp_path / "ready").exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    triggers = LocalTriggers(tmp_path / "triggers", lambda *args: None)
    try:
        triggers.recover("1234abcd")
        recover_inputs(tmp_path / "inputs", "1234abcd", set())
        path = tmp_path / "inputs" / "1234abcd" / "turn.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["events"]) == 1
        assert data["events"][0]["state"] == "unprocessed"
        assert data["events"][0]["text"] == "preserve once"
        assert triggers.records("1234abcd")[0].status == "expired"
        before = path.read_bytes()
        triggers.recover("1234abcd")
        assert recover_inputs(tmp_path / "inputs", "1234abcd", set()) == 0
        assert path.read_bytes() == before
    finally:
        triggers.close()

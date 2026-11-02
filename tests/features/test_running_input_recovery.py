"""B27.3：真实独立宿主强杀后按会话提交事实恢复输入，不重发旧补充。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adam.application.running_inputs import RunningInputs, recover_inputs
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ToolDiscoveryConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import StorageError
from adam.contracts.models import Completion
from adam.session.storage import SessionStore


@pytest.mark.parametrize("phase", ["accepted", "in_context", "committed"])
def test_real_kill_recovers_input_without_replaying(tmp_path, phase):
    host = subprocess.Popen(
        [sys.executable, "-m", "tests.features.support.running_input_worker", str(tmp_path), phase],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 8
        while not ready.exists() and host.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "宿主未抵达实际持久化边界"
        identity = json.loads(ready.read_text(encoding="utf-8"))
        host.kill()
        host.wait(timeout=5)
    finally:
        if host.poll() is None:
            host.kill()
        host.communicate(timeout=5)
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
    )

    class NoRequests:
        def complete(self, *args, **kwargs):
            pytest.fail("恢复不能请求模型")

    with SessionStore(config.sessions_dir) as store:
        app = build_application(config, store, NoRequests(), tmp_path)
        try:
            app.resume(identity["session_id"])
            path = store.root / "_inputs" / identity["session_id"] / f"{identity['turn_id']}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            assert record["status"] == ("completed" if phase == "committed" else "interrupted")
            assert record["events"][0]["state"] == (
                "unprocessed" if phase == "accepted" else "in_context"
            )
            assert record["events"][0]["text"] == "preserve this correction"
            assert bool(app.history()) == (phase == "committed")
            before = path.read_bytes()
            app.resume(identity["session_id"])
            assert path.read_bytes() == before
            assert len((tmp_path / "requests").read_text().splitlines()) == (
                1 if phase == "accepted" else 2
            )
        finally:
            app.close()


def test_input_arriving_at_tool_boundary_waits_for_complete_result(tmp_path):
    (tmp_path / "fact.txt").write_text("fact", encoding="utf-8")

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"fact.txt"}',
                                },
                            }
                        ],
                    },
                    (1, 1),
                )
            assert messages[-2]["role"] == "tool"
            assert messages[-1] == {"role": "user", "content": "supplement during tool"}
            return Completion({"role": "assistant", "content": "done"}, (1, 1))

    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    with SessionStore(config.sessions_dir) as store:
        app = build_application(
            config,
            store,
            Model(),
            tmp_path,
            live_input=True,
            on_tool=lambda _: app.submit_input("supplement during tool"),
        )
        try:
            assert app.send("read") == "done"
        finally:
            app.close()


def test_malformed_sequence_is_rejected_without_rewriting_source(tmp_path):
    inputs = RunningInputs(tmp_path, CancellationToken())
    inputs.begin("1234abcd", "turn")
    inputs.submit("kept")
    inputs.finish(False)
    path = tmp_path / "1234abcd" / "turn.json"
    record = json.loads(path.read_text())
    record["events"][0]["sequence"] = 2
    path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(StorageError):
        recover_inputs(tmp_path, "1234abcd", set())
    assert path.read_bytes() == before

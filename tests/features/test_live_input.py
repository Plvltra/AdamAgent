"""B27.2.2：真实输入泵→应用安全点→输入记录；模型调用用可控替身。"""

import json
import sys
import time
from queue import Queue
from threading import Event, Thread

import pytest

from adam.application.running_inputs import RunningInputs
from adam.bootstrap import build_application
from adam.cli.live_input import LiveInput
from adam.config.settings import AppConfig, LLMConfig, ToolDiscoveryConfig, ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, StorageError
from adam.contracts.models import Completion
from adam.session.storage import SessionStore
from adam.tool_calls.command_tools import CommandTools


@pytest.mark.parametrize("stop", [False, True])
def test_terminal_control_reaches_active_turn_and_is_recorded(tmp_path, stop):
    lines, noticed = Queue(), Event()

    def write(text):
        if "接受" in text or "停止" in text:
            noticed.set()

    terminal = LiveInput(lines.get, write)

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                lines.put("/stop\n" if stop else "/add 用中文回答\n")
                assert noticed.wait(3)
            elif not stop:
                assert messages[-1] == {"role": "user", "content": "用中文回答"}
            return Completion({"role": "assistant", "content": "answer"}, (1, 1))

    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    with SessionStore(config.sessions_dir) as store:
        app = build_application(config, store, Model(), tmp_path, live_input=True)
        terminal.start(app)
        try:
            if stop:
                with pytest.raises(RequestInterrupted):
                    app.send("question")
                assert not app.history()
            else:
                assert app.send("question") == "answer"
            files = list((store.root / "_inputs").glob("*/*.json"))
            assert len(files) == 1
            record = json.loads(files[0].read_text(encoding="utf-8"))
            assert record["status"] == ("interrupted" if stop else "completed")
            if not stop:
                assert record["events"][0]["state"] == "in_context"
                assert record["events"][0]["text"] == "用中文回答"
            else:
                assert app.send("next turn") == "answer", "新轮次必须重置旧停止信号"
            lines.put("y\n")
            assert terminal.read("approve?") == "y"
        finally:
            lines.put("")
            terminal._thread.join(timeout=3)
            app.close()


def test_failed_acceptance_is_not_queued_and_requests_stop(tmp_path, monkeypatch):
    token = CancellationToken()
    inputs = RunningInputs(tmp_path, token)
    inputs.begin("1234abcd", "turn1")

    def fail():
        raise StorageError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(inputs, "_save", fail)
        with pytest.raises(StorageError):
            inputs.submit("must not lose silently")
    assert token.cancelled and inputs.queue("turn1").peek() == ()
    inputs.finish(False)
    assert json.loads((tmp_path / "1234abcd" / "turn1.json").read_text())["events"] == []


def test_stop_token_interrupts_actual_command_wait(tmp_path):
    token, released = CancellationToken(), Event()
    ready = tmp_path / "ready"

    def stop_when_running():
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            if released.wait(0.01):
                return
        token.cancel()

    sender = Thread(target=stop_when_running)
    sender.start()
    commands = CommandTools(tmp_path, ToolLimits(), lambda _: True, cancel=token)
    try:
        with pytest.raises(RequestInterrupted):
            commands.run_command(
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import time; Path('ready').touch(); time.sleep(30)",
                    ]
                }
            )
        assert ready.exists()
    finally:
        released.set()
        sender.join(timeout=3)

"""B26.2.2：模型工具闭环、任务落盘、锁和恢复；模型回应为离线替身。"""

import json
import sys
from dataclasses import replace

import pytest

from adam.application.background import BackgroundService
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ToolDiscoveryConfig, ToolLimits
from adam.contracts.errors import CommandLogError, ToolError
from adam.contracts.models import Completion
from adam.session.storage import SessionStore
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.command_tools import CommandTools


class Model:
    def __init__(self, interrupt=False):
        self.step = 0
        self.interrupt = interrupt

    def complete(self, messages, **kwargs):
        self.step += 1
        if self.step == 1:
            assert "start_background_command" in [t["function"]["name"] for t in kwargs["tools"]]
            name = "start_background_command"
            args = {
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; print('result', flush=True); time.sleep(30)"
                    if self.interrupt
                    else "print('result')",
                ]
            }
        elif self.step == 2:
            if self.interrupt:
                raise KeyboardInterrupt()
            task = json.loads(messages[-1]["content"])["task_id"]
            name, args = "background_task", {"task_id": task, "action": "wait", "seconds": 3}
        else:
            result = json.loads(messages[-1]["content"])
            assert result["status"] == "completed" and result["exit_code"] == 0
            assert result["stdout"]["text"].strip() == "result"
            return Completion({"role": "assistant", "content": "done"}, (1, 1))
        return Completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call{self.step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            (1, 1),
        )


@pytest.mark.parametrize("interrupt", [False, True])
def test_model_tools_persist_source_and_interrupt_cleans_up(tmp_path, interrupt):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
    )
    config = replace(config, tool_discovery=ToolDiscoveryConfig(mode="full"))
    approvals = []
    with SessionStore(config.sessions_dir) as store:
        app = build_application(
            config,
            store,
            Model(interrupt),
            tmp_path,
            confirm_command=lambda cmd: approvals.append(cmd) or True,
        )
        try:
            if interrupt:
                with pytest.raises(KeyboardInterrupt):
                    app.send("run background")
            else:
                assert app.send("run background") == "done"
            sid = app.session.session_id
            paths = list((store.root / "_background" / sid).glob("*.json"))
            assert len(paths) == 1 and len(approvals) == 1
            record = json.loads(paths[0].read_text(encoding="utf-8"))
            assert record["status"] == ("cancelled" if interrupt else "completed")
            assert record["source"].keys() == {"turn_id", "request_id", "tool_call_id"}
            assert record["owner"] == sid
        finally:
            app.close()


def test_recovery_requires_lock_and_never_restarts_command(tmp_path):
    root, owner = tmp_path / "tasks", "1234abcd"
    root.joinpath(owner).mkdir(parents=True)
    task = "a" * 16
    path = root / owner / f"{task}.json"
    path.write_text(
        json.dumps({"owner": owner, "task_id": task, "status": "running"}), encoding="utf-8"
    )
    first = BackgroundService(root, BackgroundJobs(CommandTools(tmp_path, ToolLimits())))
    second = BackgroundService(root, BackgroundJobs(CommandTools(tmp_path, ToolLimits())))
    try:
        result = first.query(owner, task)
        assert result["status"] == "interrupted" and result["exit_code"] is None
        assert result["restored"]
        with pytest.raises(ToolError, match="另一个终端"):
            second.query(owner, task)
        with pytest.raises(ToolError, match="不属于"):
            first.query("8765abcd", task)
    finally:
        first.close()
        second.close()


def test_failed_intent_save_does_not_request_approval_or_start(tmp_path, monkeypatch):
    approved = []
    service = BackgroundService(
        tmp_path / "tasks",
        BackgroundJobs(
            CommandTools(tmp_path, ToolLimits(), lambda cmd: approved.append(cmd) or True)
        ),
    )

    def fail(*args):
        raise CommandLogError("injected")

    monkeypatch.setattr(service, "_save", fail)
    try:
        with pytest.raises(CommandLogError):
            service.start("1234abcd", {"argv": [sys.executable, "-c", "print(1)"]}, {})
        assert not approved
    finally:
        service.close()

"""B19.2.1 原生命令隔离的宿主契约；OS 策略另由真实 WSL 入口验收。"""

import asyncio
import sys
from threading import Timer

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import IsolationError, RequestInterrupted
from adam.isolation.command_protocol import read_request
from adam.isolation.commands import IsolatedCommands


@pytest.mark.parametrize(
    "phase",
    [
        "success",
        "nonzero",
        "cancel",
        "partial",
        "malformed",
        "wrong_policy",
        "bad_status",
        "too_large",
        "extra",
        "launcher_exit",
        "invalid_utf8",
        "bad_utf8_flag",
        "missing_utf8_flag",
    ],
)
def test_real_host_pipe_preserves_exit_and_cleans_up_without_fallback(monkeypatch, phase):
    """只替换 WSL 目标和能力探测，实际走生产编码、等待、取消和进程回收。"""
    original = asyncio.create_subprocess_exec
    runner = IsolatedCommands("Ubuntu")
    monkeypatch.setattr(runner, "probe", lambda **kwargs: True)
    monkeypatch.setenv("ADAM_FAKE_SECRET", "must-not-inherit")
    token = CancellationToken()
    processes, timers, launches = [], [], []

    async def launch(*args, **kwargs):
        launches.append((args, dict(kwargs)))
        assert "ADAM_FAKE_SECRET" not in kwargs["env"]
        kwargs.pop("env")
        process = await original(
            sys.executable,
            "-u",
            "-m",
            "tests.features.support.isolated_command_launcher",
            phase,
            **kwargs,
        )
        processes.append(process)
        if phase == "cancel":
            timer = Timer(0.3, token.cancel)
            timers.append(timer)
            timer.start()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    try:
        if phase in {"success", "nonzero", "invalid_utf8"}:
            result = runner.run(["/bin/echo", "hello"], cancel=token)
            assert result.exit_code == (7 if phase == "nonzero" else 0)
            assert result.stdout == '{"policy":"fake","exit_code":0}'
            assert result.policy == "bubblewrap-native-single-process-v1"
            assert result.reason == "exited"
            assert result.invalid_utf8 is (phase == "invalid_utf8")
        else:
            with pytest.raises(RequestInterrupted if phase == "cancel" else IsolationError):
                runner.run(["/bin/echo", "hello"], cancel=token)
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()
    assert len(processes) == 1 and processes[0].returncode is not None
    assert launches[0][0][1:5] == ("--distribution", "Ubuntu", "--exec", "/usr/bin/python3")


@pytest.mark.parametrize("argv", [[], [""], ["x\0y"], [True], ["x"] * 129, ["x", "y" * 32769]])
def test_bad_arguments_do_not_probe_or_start(monkeypatch, argv):
    runner = IsolatedCommands()

    def unexpected(**kwargs):
        pytest.fail("invalid request must not start a probe")

    monkeypatch.setattr(runner, "probe", unexpected)
    with pytest.raises(IsolationError):
        runner.run(argv)


def test_backend_unavailable_and_pre_cancel_do_not_execute(monkeypatch):
    runner = IsolatedCommands()
    probes = []
    monkeypatch.setattr(runner, "probe", lambda **kwargs: probes.append(1) or False)
    monkeypatch.setattr(runner, "_invoke", lambda *args: pytest.fail("must not execute"))
    with pytest.raises(IsolationError, match="不回退宿主"):
        runner.run(["/bin/true"])
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        runner.run(["/bin/true"], cancel=token)
    assert probes == [1]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"argv":["true"],"timeout_seconds":5,"mount":"/mnt/c"}\n',
        b'{"argv":["true"],"timeout_seconds":true}\n',
        b'{"argv":["true"],"timeout_seconds":6}\n',
        b'{"argv":["true"],"timeout_seconds":5}',
    ],
)
def test_linux_request_rejects_policy_override_and_partial_frame(raw):
    with pytest.raises(ValueError):
        read_request(raw)

"""B14.3 宿主真实进程清理；Linux 内核边界由独立 WSL 验收入口验证。"""

import asyncio
import sys
from threading import Timer

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import IsolationError, RequestInterrupted, StorageError
from adam.isolation.backend import IsolationCapability, WslBubblewrap
from adam.isolation.rpc import IsolatedOrchestrator


@pytest.mark.parametrize(
    "phase",
    ["success", "eof", "partial", "malformed", "extra", "launcher_exit", "cancel", "callback"],
)
def test_host_reaps_real_launcher_on_success_fault_and_cancel(monkeypatch, phase):
    """只替换 WSL 启动目标与探测；真实 stdin/stdout、异常传播和清理由生产宿主执行。"""
    original = asyncio.create_subprocess_exec
    backend = WslBubblewrap()
    monkeypatch.setattr(backend, "probe", lambda: IsolationCapability(True, "fixture"))
    processes, timers, calls = [], [], []
    token = CancellationToken()

    async def launch(*args, **kwargs):
        kwargs.pop("env")
        process = await original(
            sys.executable, "-u", "-m", "tests.features.support.isolation_launcher", phase, **kwargs
        )
        processes.append(process)
        if phase == "cancel":
            timer = Timer(0.3, token.cancel)
            timers.append(timer)
            timer.start()
        return process

    def invoke(name, arguments):
        calls.append((name, arguments))
        if phase == "callback":
            raise StorageError("injected storage failure")
        return {"ok": True, "size": 3}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    try:
        runner = IsolatedOrchestrator(backend)
        if phase == "success":
            assert runner.run("result = 1", invoke, cancel=token).result == {"count": 1}
        else:
            expected = (
                RequestInterrupted
                if phase == "cancel"
                else StorageError
                if phase == "callback"
                else IsolationError
            )
            with pytest.raises(expected):
                runner.run("result = 1", invoke, cancel=token)
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()
    assert len(processes) == 1 and processes[0].returncode is not None
    assert len(calls) == (1 if phase in {"success", "extra", "launcher_exit", "callback"} else 0)

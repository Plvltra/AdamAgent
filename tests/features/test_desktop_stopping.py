"""桌面租约跨进程竞争及取消/失焦恢复；不产生截图或操纵真实桌面。"""

import subprocess
import sys
from dataclasses import replace

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.desktop.lease import DesktopLease
from adam.tool_calls.desktop_tools import DesktopTools
from tests.features.test_desktop_actions import Actions
from tests.features.test_media_evidence import active_media


def test_lease_refuses_another_process_and_releases_after_failure(tmp_path):
    path = tmp_path / "desktop.lock"
    code = """import sys
from pathlib import Path
from adam.desktop.lease import DesktopLease
from adam.contracts.errors import ToolError
try:
 with DesktopLease(Path(sys.argv[1])).hold(): print("acquired")
except ToolError: print("busy")
"""
    lease = DesktopLease(path)
    with pytest.raises(RuntimeError), lease.hold():
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True
        )
        assert result.stdout.strip() == "busy"
        raise RuntimeError("injected failure while leased")
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "acquired"


@pytest.mark.parametrize("failure", ["cancel", "focus", "busy"])
def test_interrupted_action_invalidates_reference_and_needs_new_observation(tmp_path, failure):
    backend = Actions()
    with active_media(tmp_path, kind="tool") as (store, source, media):
        # 测试来源真实属于test_download；此处固定工具源验证，专测租约/恢复协调而非注册名。
        tools = DesktopTools(backend, media, lambda plan: True, 16000)
        tools.lease = DesktopLease(tmp_path / "lease.lock")
        tools.bind_source(
            {
                "turn_id": source.turn_id,
                "request_id": source.request_id,
                "tool_call_id": source.tool_call_id,
            }
        )
        # 通过宿主内部观察建立当前引用，来源仍由真实SessionMedia校验。
        first = tools._observe(source.session_id, source, CancellationToken(), "observed")
        action = {"version": first["version"], "operation": "key", "value": "TAB"}
        token = CancellationToken()
        if failure == "cancel":
            token.cancel()
            with pytest.raises(RequestInterrupted):
                tools.invoke("test_download", action, None, token)
        elif failure == "focus":
            backend.frame = replace(backend.frame, title="lost focus or changed")
            with pytest.raises(ToolError, match="changed"):
                tools.invoke("test_download", action, None, token)
        else:
            with tools.lease.hold(), pytest.raises(ToolError, match="leased"):
                tools.invoke("test_download", action, None, token)
        assert backend.actions == [] and tools.last is None
        with pytest.raises(ToolError, match="Stale"):
            tools.invoke("test_download", action, None, CancellationToken())
        renewed = tools._observe(source.session_id, source, CancellationToken(), "observed")
        tools.invoke(
            "test_download", {**action, "version": renewed["version"]}, None, CancellationToken()
        )
        assert len(backend.actions) == 1


def test_focus_guard_latches_loss_until_explicit_observation(monkeypatch):
    from threading import Event, Thread

    from adam.desktop.adapters import windows

    class NativeCall:
        def __call__(self):
            return 123

    class Native:
        GetForegroundWindow = NativeCall()

    lost, recovered = Event(), Event()

    def desktop_check():
        lost.set()
        if not recovered.is_set():
            raise ToolError("simulated locked input desktop")

    monkeypatch.setattr(windows.ctypes, "WinDLL", lambda *args, **kwargs: Native())
    monkeypatch.setattr(windows, "require_input_desktop", desktop_check)
    backend = windows.WindowsDesktop(123, 42)
    backend._invalidated.clear()
    worker = Thread(target=backend._watch)
    worker.start()
    try:
        assert lost.wait(2)
        # 等实际看守处理完本次异常，再恢复，确认恢复不会自行解除失效。
        assert backend._invalidated.wait(2)
        recovered.set()
        assert backend._invalidated.is_set()
    finally:
        backend._watch_stop.set()
        worker.join(2)
    assert not worker.is_alive()


def test_interruption_between_tools_closes_desktop_resource(tmp_path):
    from tests.features.test_browser_actions import application
    from tests.features.test_desktop_actions import config
    from tests.features.test_subagents import call
    from tests.features.test_tool_loop import tool_response

    class ClosingBackend(Actions):
        closed = 0

        def close(self):
            self.closed += 1

    backend = ClosingBackend()

    def respond(body, number):
        if number == 1:
            return tool_response([call("observe", "desktop_observe")])
        raise RequestInterrupted()

    with application(tmp_path, respond, config(tmp_path), desktop_backend=backend) as (app, _, _):
        with pytest.raises(RequestInterrupted):
            app.send("observe then interrupt")
        assert backend.closed == 1
        assert backend.actions == []


@pytest.mark.parametrize(
    "level,state,flags,allowed",
    [(1, 0, 1, True), (1, 0, 0, False), (1, 4, 1, False), (2, 0, 1, False)],
)
def test_session_lock_status_requires_explicit_active_unlocked_and_frees_buffer(
    monkeypatch, level, state, flags, allowed
):
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    from adam.desktop.adapters import input_desktop

    record = input_desktop._InfoPrefix()
    record.level = level
    record.data.session.state = state
    record.data.session.flags = flags
    freed = []

    def query(server, session, kind, output, length):
        assert session == 0xFFFFFFFF and kind == 25
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(record)
        ctypes.cast(length, ctypes.POINTER(wintypes.DWORD))[0] = ctypes.sizeof(record)
        return True

    api = SimpleNamespace(
        WTSQuerySessionInformationW=query, WTSFreeMemory=lambda pointer: freed.append(pointer.value)
    )
    monkeypatch.setattr(input_desktop.ctypes, "WinDLL", lambda *args, **kwargs: api)
    monkeypatch.setattr(input_desktop.sys, "getwindowsversion", lambda: SimpleNamespace(major=10))
    if allowed:
        input_desktop.require_unlocked_session()
    else:
        with pytest.raises(ToolError, match="locked"):
            input_desktop.require_unlocked_session()
    assert freed == [ctypes.addressof(record)]

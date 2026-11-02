"""B21.2.1：真实固定正则子进程的匹配、输入边界、灾难回溯及执行中取消。"""

import os
import subprocess
import time
from threading import Event, Thread

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted, ToolError
from adam.tool_calls.regex_match import match_lines
from adam.tool_calls.regex_worker import MAX_TEXT_BYTES


@pytest.mark.parametrize(
    "pattern,text,lines",
    [
        (r"^value = \d+$", "no\nvalue = 12\r\nvalue = xx\rvalue = 3", (2, 4)),
        ("汉字|é", "汉字\né\nother\n", (1, 2)),
        ("(?i)needle", "NEEDLE\nnone", (1,)),
        ("^$", "\nempty\n\n", (1, 3)),
        (".*", "", ()),
        (r"a\.b", "a.b\naxb", (1,)),
    ],
)
def test_real_worker_preserves_line_numbers_and_regex_semantics(pattern, text, lines):
    assert match_lines(pattern, text, timeout_seconds=3) == lines


def test_invalid_expression_fails_without_returning_lines_or_echoing_pattern():
    with pytest.raises(ToolError, match="正则表达式无效") as error:
        match_lines("[private-pattern", "private-text", timeout_seconds=3)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "pattern,text,timeout",
    [
        ("", "text", 1),
        ("a" * 1025, "", 1),
        ("x", "", 0),
        ("x", "", True),
        ("x", "", float("nan")),
        ("x", "a" * (MAX_TEXT_BYTES + 1), 1),
        ("x", "\ud800", 1),
    ],
    ids=["empty", "long-pattern", "zero-time", "bool-time", "nan-time", "large-text", "bad-utf8"],
)
def test_bounds_fail_before_process_creation(monkeypatch, pattern, text, timeout):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ToolError):
        match_lines(pattern, text, timeout_seconds=timeout)


def test_catastrophic_backtracking_times_out_and_reaps_real_process(monkeypatch):
    original, processes = subprocess.Popen, []

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture)
    started = time.monotonic()
    with pytest.raises(ToolError, match="超时"):
        match_lines("(a+)+$", "a" * 100000 + "!", timeout_seconds=0.5)
    assert 0.4 <= time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None


def test_cancel_after_worker_has_received_request_reaps_process(monkeypatch):
    original, processes, streams = subprocess.Popen, [], []
    started, received = Event(), Event()
    cancel = CancellationToken()

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        streams.append(kwargs["stdout"])
        started.set()
        return process

    def interrupt():
        if not started.wait(3):
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            # 只查文件大小，不改变共享文件偏移；READY是在读入请求后、匹配前写出的。
            if os.fstat(streams[0].fileno()).st_size >= 2:
                received.set()
                cancel.cancel()
                return
            time.sleep(0.01)

    monkeypatch.setattr(subprocess, "Popen", capture)
    controller = Thread(target=interrupt)
    controller.start()
    try:
        with pytest.raises(RequestInterrupted):
            match_lines("(a+)+$", "a" * 100000 + "!", timeout_seconds=5, cancel=cancel)
    finally:
        controller.join(timeout=4)
    assert received.is_set() and not controller.is_alive()
    assert len(processes) == 1 and processes[0].poll() is not None


def test_pre_cancelled_match_never_starts_worker(monkeypatch):
    cancel = CancellationToken()
    cancel.cancel()

    def forbidden(*args, **kwargs):
        pytest.fail("pre-cancelled work started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(RequestInterrupted):
        match_lines("x", "x", cancel=cancel)

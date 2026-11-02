"""B18.1：实际进程头尾与字节计数，管道替身只用于精确断裂和读取失败边界。"""

import io
import sys
from threading import Event, Thread

import pytest

from adam.config.settings import ToolLimits
from adam.tool_calls.command_output import OutputCapture
from adam.tool_calls.command_tools import CommandTools


def test_real_command_keeps_start_last_error_and_exact_omitted_bytes(tmp_path):
    raw = b"BEGIN\n" + b"log\n" * 10000 + b"LAST-ERROR\n"
    tools = CommandTools(tmp_path, ToolLimits(), lambda _: True, capture_bytes=128)
    events = []
    result = tools.run_command(
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys; "
                "sys.stdout.buffer.write(b'BEGIN\\n'+b'log\\n'*10000+b'LAST-ERROR\\n'); "
                "sys.stderr.write('reason'); sys.exit(7)",
            ]
        },
        lambda kind, value: events.append((kind, value)),
    )
    assert result["stdout"].startswith("BEGIN\n")
    assert result["stdout"].endswith("LAST-ERROR\n")
    assert result["stderr"] == "reason" and result["exit_code"] == 7
    assert result["ok"] and result["truncated"]
    assert result["output_capture"]["stdout"] == {
        "read_bytes": len(raw),
        "omitted_bytes": len(raw) - 128,
        "eof": True,
        "invalid_utf8": False,
    }
    assert events[-1][0] == "command_finished"
    assert events[-1][1]["stdout"].endswith("LAST-ERROR\n")
    assert events[-1][1]["output_capture"] == result["output_capture"]


@pytest.mark.parametrize("size", [0, 3, 17, 18, 19, 4097, 20000])
def test_pipe_capacity_preserves_small_content_and_counts_large_content(size):
    capture = OutputCapture(18)
    raw = bytes(65 + i % 26 for i in range(size))
    capture.drain(io.BytesIO(raw))
    result = capture.snapshot()
    assert result.read_bytes == size and result.eof and not result.invalid_utf8
    assert result.omitted_bytes == max(0, size - 18)
    if size <= 18:
        assert result.text.encode() == raw and not result.truncated
    else:
        assert result.text.startswith(raw[:9].decode())
        assert result.text.endswith(raw[-9:].decode())
        assert result.truncated


@pytest.mark.parametrize("limit", [7, 8, 9, 10, 11, 12, 13, 128])
def test_multibyte_cut_is_counted_without_replacement_characters(limit):
    raw = ("汉😀abc" * 900).encode()
    capture = OutputCapture(limit)
    capture.drain(io.BytesIO(raw))
    result = capture.snapshot()
    prefix, suffix = result.text.split("\n[...省略 ", 1)
    suffix = suffix.split(" 字节...]\n", 1)[1]
    assert "\ufffd" not in result.text and not result.invalid_utf8
    assert raw.startswith(prefix.encode()) and raw.endswith(suffix.encode())
    assert result.omitted_bytes == len(raw) - len((prefix + suffix).encode())
    assert len((prefix + suffix).encode()) <= limit


def test_invalid_original_utf8_is_explicit_even_without_capacity_loss():
    capture = OutputCapture(100)
    capture.drain(io.BytesIO(b"begin\xffend"))
    result = capture.snapshot()
    assert result.text == "begin\ufffdend"
    assert result.truncated and result.invalid_utf8 and result.omitted_bytes == 0
    assert result.read_bytes == 9 and result.eof


def test_io_failure_keeps_observed_count_but_does_not_claim_end_of_output():
    class Broken(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("injected pipe read failure")
            return super().read(size)

    capture = OutputCapture(8)
    capture.drain(Broken(b"first"))
    result = capture.snapshot()
    assert result.text == "first" and result.read_bytes == 5
    assert result.omitted_bytes == 0 and result.truncated and not result.eof


def test_snapshot_while_pipe_is_open_remains_immutable_and_incomplete():
    ready, release = Event(), Event()

    class Paused(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                ready.set()
                assert release.wait(5)
            return super().read(size)

    capture = OutputCapture(128)
    reader = Thread(target=capture.drain, args=(Paused(b"first"),))
    reader.start()
    try:
        assert ready.wait(5)
        first = capture.snapshot()
        assert first.text == "first" and not first.eof and first.truncated
    finally:
        release.set()
        reader.join(5)
    assert not reader.is_alive()
    assert capture.snapshot().eof and not first.eof

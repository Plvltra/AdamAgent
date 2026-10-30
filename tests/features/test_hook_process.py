"""T5.1：真实本地检查器通过 JSON stdin/stdout 通信，失败不伪装为允许执行。"""

import json
import os
import sys
import threading
import time
from dataclasses import replace

import pytest

from adam.config.hooks import HookCommand, HooksConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.contracts.execution import ExecutionMode
from adam.contracts.hooks import HookEvent, HookRequest
from adam.hooks.runner import HookRunner
from tests.features.test_cancellation import alive


def request(event=HookEvent.BEFORE_TOOL):
    return HookRequest(
        event,
        "read_file",
        {"path": "资料.txt"},
        {"session_id": "s1", "turn_id": "t1", "request_id": "r1", "tool_call_id": "c1"},
        ExecutionMode.PLAN,
        {"ok": True, "content": "事实"} if event is HookEvent.AFTER_TOOL else None,
    )


def checker(root, source):
    """写入真实检查脚本；使用 argv 保留带空格参数，不经过 shell。"""
    path = root / "local checker.py"
    path.write_text(source, encoding="utf-8")
    return HookCommand((sys.executable, str(path), "argument with spaces"))


@pytest.mark.parametrize(
    "decision,event,expected",
    [
        ("allow", HookEvent.BEFORE_TOOL, "allowed"),
        ("deny", HookEvent.BEFORE_TOOL, "denied"),
        (None, HookEvent.AFTER_TOOL, "observed"),
    ],
)
def test_local_checker_receives_snapshot_and_no_arbitrary_environment(
    tmp_path,
    monkeypatch,
    decision,
    event,
    expected,
):
    monkeypatch.setenv("ADAM_HOOK_SECRET", "must-not-leak")
    reply = (
        {"version": 1, "decision": decision, "reason": "检查完成"}
        if decision
        else {"version": 1, "note": "已记录事实"}
    )
    command = checker(
        tmp_path,
        (
            "import json, os, sys\nfrom pathlib import Path\n"
            "payload = json.load(sys.stdin)\n"
            "record = {'input': payload, 'secret': os.environ.get('ADAM_HOOK_SECRET'), "
            "'argument': sys.argv[1], 'cwd': os.getcwd()}\n"
            "Path('received.json').write_text(json.dumps(record), encoding='utf-8')\n"
            f"print(json.dumps({reply!r}, ensure_ascii=False))\n"
            "print('checker diagnostic', file=sys.stderr)\n"
        ),
    )
    config = HooksConfig(True, before_tool=command, after_tool=command)
    result = HookRunner(config, tmp_path).run(request(event))
    assert result.status == expected and result.exit_code == 0 and not result.error_code
    assert result.duration_ms >= 0 and result.stderr.strip() == "checker diagnostic"
    received = json.loads((tmp_path / "received.json").read_text(encoding="utf-8"))
    assert received["secret"] is None and received["argument"] == "argument with spaces"
    assert os.path.samefile(received["cwd"], tmp_path)
    assert received["input"]["tool"] == {"name": "read_file", "arguments": {"path": "资料.txt"}}
    assert received["input"]["source"] == request().source
    assert received["input"]["execution_mode"] == "plan"
    assert ("result" in received["input"]) is (event is HookEvent.AFTER_TOOL)
    if event is HookEvent.AFTER_TOOL:
        assert received["input"]["result"] == request(event).result


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_unconfigured_event_has_no_process_side_effect(tmp_path, enabled):
    command = checker(tmp_path, "from pathlib import Path\nPath('should-not-run').touch()\n")
    config = HooksConfig(enabled, after_tool=command)
    assert HookRunner(config, tmp_path).run(request()) is None
    assert not (tmp_path / "should-not-run").exists()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("print('not JSON')", "invalid_response"),
        ('print(\'{"version":1,"decision":"allow"}\'); sys.exit(3)', "nonzero_exit"),
        ("time.sleep(30)", "timed_out"),
        ("print('x' * 50000)", "output_limit"),
        ("print('x' * 50000, file=sys.stderr)", "output_limit"),
    ],
)
def test_process_failure_has_stable_status_and_is_reaped(tmp_path, code, expected):
    command = checker(
        tmp_path,
        (
            "import json, os, sys, time\nfrom pathlib import Path\n"
            "json.load(sys.stdin)\nPath('pid').write_text(str(os.getpid()))\n" + code + "\n"
        ),
    )
    config = HooksConfig(True, before_tool=command, timeout_seconds=1, max_output_bytes=256)
    result = HookRunner(config, tmp_path).run(request())
    assert result.status == "failed" and result.error_code == expected
    assert len(result.stderr.encode("utf-8")) <= config.max_output_bytes
    assert not alive(int((tmp_path / "pid").read_text()))
    if expected == "nonzero_exit":
        assert result.exit_code == 3


def test_invalid_or_oversized_input_does_not_start_checker(tmp_path):
    command = checker(tmp_path, "from pathlib import Path\nPath('unexpected').touch()\n")
    runner = HookRunner(HooksConfig(True, before_tool=command, max_input_bytes=256), tmp_path)
    result = runner.run(replace(request(), arguments={"content": "x" * 1000}))
    assert result.status == "failed" and result.error_code == "invalid_input"
    assert result.exit_code is None and not (tmp_path / "unexpected").exists()


def test_missing_program_is_reported_without_retry(tmp_path):
    runner = HookRunner(
        HooksConfig(True, before_tool=HookCommand(("AdamHookProgramDoesNotExist.exe",))),
        tmp_path,
    )
    result = runner.run(request())
    assert result.status == "failed" and result.error_code == "start_failed"
    assert result.exit_code is None


def test_checker_not_reading_stdin_can_be_cancelled_and_reaped(tmp_path):
    command = checker(
        tmp_path,
        (
            "import os, time\nfrom pathlib import Path\n"
            "Path('ready').write_text(str(os.getpid()))\ntime.sleep(30)\n"
        ),
    )
    cancel = CancellationToken()
    ready = tmp_path / "ready"
    observed = []

    def stop_after_started():
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        observed.append(ready.exists())
        cancel.cancel()

    watcher = threading.Thread(target=stop_after_started)
    watcher.start()
    runner = HookRunner(
        HooksConfig(True, before_tool=command, timeout_seconds=15, max_input_bytes=1048576),
        tmp_path,
        cancel,
    )
    try:
        with pytest.raises(RequestInterrupted):
            # 大于管道容量；检查器不读 stdin，输入线程不能阻止主线程取消。
            runner.run(replace(request(), arguments={"content": "x" * 400000}))
    finally:
        cancel.cancel()
        watcher.join(timeout=12)
    assert observed == [True] and not watcher.is_alive()
    assert not alive(int(ready.read_text()))

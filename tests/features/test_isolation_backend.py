"""B19.1 宿主边界契约；替身仅测通信，不把这些测试当成 OS 隔离验收。"""

import subprocess

import pytest

from adam.isolation.backend import CHECKS, POLICY, IsolationError, WslBubblewrap


def available():
    return {
        "policy": POLICY,
        "available": True,
        "checks": dict.fromkeys(CHECKS, True),
        "exit_code": 0,
        "reason": "exited",
    }


@pytest.mark.parametrize("name", ["", "--help", "Ubuntu;echo", "a" * 65, "../Ubuntu"])
def test_distribution_is_explicit_and_not_an_option_or_shell_fragment(name):
    with pytest.raises(IsolationError):
        WslBubblewrap(name)


@pytest.mark.parametrize(
    "code", ["", "x" * 65537, "\ud800", None], ids=["empty", "large", "unicode", "type"]
)
def test_invalid_code_never_starts_probe_or_worker(monkeypatch, code):
    monkeypatch.setattr(WslBubblewrap, "_invoke", lambda *args: pytest.fail("must not execute"))
    with pytest.raises(IsolationError):
        WslBubblewrap().run_python(code)


@pytest.mark.parametrize("fault", ["missing", "false", "not_bool", "extra", "exit", "exception"])
def test_failed_or_incomplete_probe_cannot_fall_back_to_host(monkeypatch, fault):
    calls = []
    value = available()
    if fault == "missing":
        value["checks"].pop("network_denied")
    elif fault in {"false", "not_bool"}:
        value["checks"]["network_denied"] = False if fault == "false" else 1
    elif fault == "extra":
        value["checks"]["invented"] = True
    elif fault == "exit":
        value["exit_code"] = 1

    def invoke(self, operation, payload):
        calls.append(operation)
        if fault == "exception":
            raise IsolationError("missing backend")
        return value

    monkeypatch.setattr(WslBubblewrap, "_invoke", invoke)
    with pytest.raises(IsolationError, match="不回退"):
        WslBubblewrap().run_python("print('never executed')")
    assert calls == ["probe"]


@pytest.mark.parametrize(
    "reason,exit_code", [("exited", 0), ("exited", 1), ("wall_limit", -9), ("output_limit", -9)]
)
def test_exit_facts_and_bounded_output_are_preserved(monkeypatch, reason, exit_code):
    calls = []

    def invoke(self, operation, payload):
        calls.append((operation, payload))
        if operation == "probe":
            return available()
        return {
            "policy": POLICY,
            "reason": reason,
            "exit_code": exit_code,
            "stdout": "partial",
            "stderr": "failure",
            "duration_ms": 12,
        }

    monkeypatch.setattr(WslBubblewrap, "_invoke", invoke)
    result = WslBubblewrap().run_python("print('你好')")
    assert result.reason == reason and result.exit_code == exit_code
    assert result.stdout == "partial" and result.stderr == "failure"
    assert calls == [("probe", b""), ("run", "print('你好')".encode())]


@pytest.mark.parametrize("fault", ["fields", "output", "duration", "exit"])
def test_corrupt_result_is_unknown_and_is_not_retried(monkeypatch, fault):
    calls = []
    value = {
        "policy": POLICY,
        "reason": "exited",
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "duration_ms": 1,
    }
    if fault == "fields":
        value.pop("stderr")
    elif fault == "output":
        value["stdout"] = "x" * 65537
    elif fault == "duration":
        value["duration_ms"] = True
    else:
        value["exit_code"] = True

    def invoke(self, operation, payload):
        calls.append(operation)
        return available() if operation == "probe" else value

    monkeypatch.setattr(WslBubblewrap, "_invoke", invoke)
    with pytest.raises(IsolationError, match="结果未知"):
        WslBubblewrap().run_python("print('once')")
    assert calls == ["probe", "run"]


def test_transport_does_not_inherit_secrets_or_use_shell(monkeypatch):
    monkeypatch.setenv("ADAM_API_KEY", "private-test-secret")
    monkeypatch.setenv("WSLENV", "ADAM_API_KEY/u")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr("adam.isolation.backend.subprocess.run", run)
    assert not WslBubblewrap("Ubuntu").probe().available
    if calls:  # 非 Windows 宿主在调用 WSL 前就明确拒绝。
        argv, options = calls[0]
        assert "--distribution" in argv and "Ubuntu" in argv
        assert options.get("shell", False) is False
        assert set(options["env"]) == {"PATH", "SystemRoot"}
        assert b"private-test-secret" not in options["input"]

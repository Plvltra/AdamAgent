"""B14.1 协议与真实注册工具桥；远端隔离进程另用显式入口实测，不能用替身冒充。"""

import json

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.contracts.execution import ExecutionMode
from adam.isolation.rpc_protocol import RpcProtocolError, decode_frame, encode_frame, validate_call
from adam.tool_calls.code_bridge import RegisteredToolBridge
from adam.tool_calls.factory import create_tool_registry


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}\n',
        b'{"x":NaN}\n',
        b'{"x":Infinity}\n',
        b"[]\n",
        b"{}",
        b'{"x":' + b"9" * 65 + b"}\n",
        b'{"x":' + b"[" * 40 + b"0" + b"]" * 40 + b"}\n",
        b'{"x":"' + b"x" * 65536 + b'"}\n',
    ],
    ids=["duplicate", "nan", "infinite", "list", "partial", "integer", "deep", "large"],
)
def test_invalid_frames_cannot_become_tool_arguments(raw):
    with pytest.raises(RpcProtocolError):
        decode_frame(raw)


def test_valid_frames_preserve_exact_arguments_without_executing_them():
    value = {"kind": "call", "sequence": 1, "name": "read_file", "arguments": {"path": "示例.md"}}
    assert validate_call(decode_frame(encode_frame(value)), 1) == ("read_file", {"path": "示例.md"})


@pytest.mark.parametrize("sequence,expected", [(1, 2), (True, 1), (17, 17), (0, 0)])
def test_duplicate_boolean_or_excess_calls_stop_the_protocol(sequence, expected):
    with pytest.raises(RpcProtocolError):
        validate_call(
            {"kind": "call", "sequence": sequence, "name": "read_file", "arguments": {}}, expected
        )


def test_real_registered_reads_preserve_validation_and_path_authority(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("outside authority", encoding="utf-8")
    bridge = RegisteredToolBridge(create_tool_registry(workspace), ("read_file", "file_info"))
    assert bridge("file_info", {"path": "notes.txt"})["ok"]
    assert bridge("read_file", {"path": "notes.txt"})["lines"][1]["text"] == "beta"
    assert not bridge("read_file", {"path": str(outside)})["ok"]
    assert not bridge("read_file", {"path": 12})["ok"]
    assert not bridge("run_command", {"argv": ["git", "--version"]})["ok"]
    assert outside.read_text(encoding="utf-8") == "outside authority"


def test_model_cannot_expand_allowed_tools_or_bypass_plan_mode(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    approvals = []
    registry = create_tool_registry(
        tmp_path, confirm_edit=lambda plan: approvals.append(plan) or True
    )
    version = json.loads(registry.execute("read_file", '{"path":"a.txt"}'))["version"]
    with pytest.raises(ValueError):
        RegisteredToolBridge(registry, ("not_registered",))
    with pytest.raises(ValueError):
        RegisteredToolBridge(registry, ("read_file", "read_file"))
    bridge = RegisteredToolBridge(registry.with_mode(lambda: ExecutionMode.PLAN), ("replace_text",))
    result = bridge(
        "replace_text",
        {"path": "a.txt", "old_text": "before", "new_text": "after", "expected_version": version},
    )
    assert not result["ok"] and result["status"] == "mode_blocked" and not approvals
    assert path.read_text(encoding="utf-8") == "before"


def test_bridge_keeps_original_before_tool_and_propagates_cancellation(tmp_path):
    (tmp_path / "a.txt").write_text("text", encoding="utf-8")
    token = CancellationToken()
    calls = []
    bridge = RegisteredToolBridge(
        create_tool_registry(tmp_path),
        ("read_file",),
        cancel=token,
        before_tool=lambda name, args, mode: calls.append((name, args)),
    )
    assert bridge("read_file", {"path": "a.txt"})["ok"]
    assert calls == [("read_file", {"path": "a.txt"})]
    token.cancel()
    with pytest.raises(RequestInterrupted):
        bridge("read_file", {"path": "a.txt"})
    assert len(calls) == 1


def test_runtime_failure_stays_a_tool_result_without_bridge_retry(tmp_path):
    calls = []
    bridge = RegisteredToolBridge(
        create_tool_registry(tmp_path),
        ("read_file",),
        before_tool=lambda name, args, mode: calls.append(name),
    )
    result = bridge("read_file", {"path": "missing.txt"})
    assert result["ok"] is False and "error" in result
    assert json.loads(json.dumps(result)) == result
    assert calls == ["read_file"]

"""B19.2.2 显式文件选择、来源复查和隔离数据协议；真实挂载另验收。"""

import base64
import hashlib
import json

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_inputs import CommandInput
from adam.contracts.errors import IsolationError, RequestInterrupted, ToolError
from adam.isolation.command_protocol import read_request, request
from adam.isolation.commands import IsolatedCommands
from adam.tool_calls.command_inputs import capture_inputs
from adam.tool_calls.policy import PathPolicy


def test_selected_bytes_are_immutable_and_wire_contains_no_host_source(tmp_path):
    first, second = tmp_path / "a.bin", tmp_path / "b.txt"
    first.write_bytes(b"\0\xff\xfe")
    second.write_text("只读输入", encoding="utf-8")
    (tmp_path / "not-selected.txt").write_text("private sentinel", encoding="utf-8")
    policy = PathPolicy(tmp_path, PermissionConfig())
    captured = capture_inputs(policy, {"src/a.bin": "a.bin", "b.txt": "b.txt"})
    assert [item.name for item in captured.files] == ["b.txt", "src/a.bin"]
    assert captured.files[1].data == b"\0\xff\xfe"
    raw = request(["cat", "src/a.bin"], 5, captured.files)
    assert str(tmp_path).encode() not in raw and b"private sentinel" not in raw
    assert read_request(raw) == (["cat", "src/a.bin"], 5, captured.files)
    captured.verify(policy)
    first.write_bytes(b"new source")
    assert captured.files[1].data == b"\0\xff\xfe"
    with pytest.raises(ToolError, match="已变化"):
        captured.verify(policy)


@pytest.mark.parametrize(
    "name", ["/root", "../a", "a/../b", "a\\b", "a//b", "a/", "./a", "C:a", "x\0y"]
)
def test_virtual_paths_cannot_escape_private_snapshot(tmp_path, name):
    policy = PathPolicy(tmp_path, PermissionConfig())
    with pytest.raises(ToolError, match="名称"):
        capture_inputs(policy, {name: "missing.txt"})


def test_prefix_conflict_and_count_rejected_before_open(tmp_path):
    policy = PathPolicy(tmp_path, PermissionConfig())
    for names in ({"a": "missing", "a/b": "missing"}, {f"file-{i}": "missing" for i in range(17)}):
        with pytest.raises(ToolError, match="名称"):
            capture_inputs(policy, names)


@pytest.mark.parametrize("name", ["llm.toml", ".env", "id_rsa", "private.key", ".aws/key"])
def test_secret_paths_keep_original_denial(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret sentinel", encoding="utf-8")
    policy = PathPolicy(tmp_path, PermissionConfig())
    with pytest.raises(ToolError, match="凭据"):
        capture_inputs(policy, {"safe-name.txt": name})


def test_extra_root_needs_authorization_and_revocation_stops_old_snapshot(tmp_path):
    workspace, external = tmp_path / "project", tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "notes.txt"
    source.write_text("allowed", encoding="utf-8")
    denied = PathPolicy(workspace, PermissionConfig())
    with pytest.raises(ToolError, match="未授权"):
        capture_inputs(denied, {"notes.txt": str(source)})
    allowed = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(external,)))
    batch = capture_inputs(allowed, {"notes.txt": str(source)})
    assert batch.files[0].data == b"allowed"
    with pytest.raises(ToolError, match="未授权"):
        batch.verify(denied)
    protected = PathPolicy(
        workspace, PermissionConfig(additional_read_dirs=(external,)), protected_dirs=(external,)
    )
    with pytest.raises(ToolError, match="保留目录"):
        batch.verify(protected)


def test_input_file_and_total_limits_do_not_return_partial_batch(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 1048576)
    policy = PathPolicy(tmp_path, PermissionConfig())
    with pytest.raises(ToolError, match="大小超限"):
        capture_inputs(policy, {"small": "large.txt"}, max_file_bytes=4)
    with pytest.raises(ToolError, match="总量"):
        capture_inputs(policy, {f"copy-{i}": "large.txt" for i in range(3)})
    batch = capture_inputs(policy, {"copy-1": "large.txt", "copy-2": "large.txt"})
    assert sum(len(item.data) for item in batch.files) == 2097152


def test_source_changes_during_read_and_cancel_are_not_accepted(tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"first")
    policy = PathPolicy(tmp_path, PermissionConfig())
    original = policy.resolve
    calls = 0

    def changing(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b"different version")
        return original(value)

    monkeypatch.setattr(policy, "resolve", changing)
    with pytest.raises(ToolError, match="读取时变化"):
        capture_inputs(policy, {"notes.txt": "notes.txt"})
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        capture_inputs(policy, {"notes.txt": "notes.txt"}, cancel=token)
    assert calls == 2


@pytest.mark.parametrize("change", ["hash", "path", "base64", "mount", "duplicate"])
def test_corrupted_input_wire_rejected_by_linux_before_materializing(change):
    data = b"original"
    item = CommandInput("data/input.txt", data, hashlib.sha256(data).hexdigest())
    payload = json.loads(request(["cat", "data/input.txt"], 5, (item,)))
    if change == "hash":
        payload["inputs"][0]["data"] = base64.b64encode(b"changed").decode()
    elif change == "path":
        payload["inputs"][0]["name"] = "../escape"
    elif change == "base64":
        payload["inputs"][0]["data"] = "not base64!"
    elif change == "mount":
        payload["mount"] = "/mnt/c"
    else:
        payload["inputs"].append(payload["inputs"][0])
    with pytest.raises(ValueError):
        read_request(json.dumps(payload).encode() + b"\n")


def test_forged_snapshot_cannot_probe_or_execute(monkeypatch):
    runner = IsolatedCommands()
    monkeypatch.setattr(runner, "probe", lambda **kwargs: pytest.fail("must reject before probe"))
    with pytest.raises(IsolationError):
        runner.run(["cat", "input"], inputs=(CommandInput("input", b"data", "fake-version"),))

"""T12.1 发现策略：真实注册表与文件权限，聊天动态接入另由 T12.2 验收。"""

import json
from dataclasses import asdict

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.tool_catalog import ToolLoadingMode
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery import ToolVisibility
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.registry import ToolRegistry


def test_real_directory_discovers_groups_without_running_tools_then_reads_normally(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("real evidence", encoding="utf-8")
    touched = []
    registry = create_tool_registry(tmp_path, confirm_edit=lambda p: touched.append(p) or True)
    registry = registry.guarded(lambda paths: touched.append(paths))
    visibility = ToolVisibility(registry.directory(), ExecutionMode.ACT)
    listings = {entry.name: entry for entry in visibility.listings()}
    assert listings["read_file"].group == "files"
    assert listings["git_status"].group == "git"
    assert listings["replace_text"].group == "editing"
    assert all("parameters" not in asdict(entry) for entry in listings.values())
    initial = visibility.snapshot()
    assert initial.definitions() == []
    added = visibility.discover(names=("read_file", "read_file"), groups=("git", "git"))
    assert set(added) == {"read_file", "git_status", "git_diff", "git_log"}
    assert visibility.discover(names=("read_file",), groups=("git",)) == ()
    assert touched == [] and target.read_text(encoding="utf-8") == "real evidence"
    with pytest.raises(ToolError, match="尚未提供"):
        initial.require("read_file")
    current = visibility.snapshot()
    current.require("read_file")
    assert len(current.names) == len(set(current.names))
    read = json.loads(registry.execute("read_file", json.dumps({"path": str(target)})))
    assert read["ok"] and read["lines"][0]["text"] == "real evidence"
    assert len(touched) == 1


@pytest.mark.parametrize(
    ("names", "groups"),
    [
        (("read_file", "not_registered"), ()),
        (("read_file",), ("missing",)),
        (("read_file", "replace_text"), ()),
        ((), ("files", "editing")),
        ((), ()),
        ((False,), ()),
    ],
)
def test_bad_or_mode_hidden_selection_is_atomic(tmp_path, names, groups):
    visibility = ToolVisibility(create_tool_registry(tmp_path).directory(), ExecutionMode.PLAN)
    visibility.discover(names=("file_info",))
    before = visibility.snapshot()
    with pytest.raises(ToolError):
        visibility.discover(names=names, groups=groups)
    assert visibility.snapshot() == before


def test_capability_not_tool_name_group_or_description_controls_plan_visibility(tmp_path):
    registry = create_tool_registry(tmp_path).extended(
        {
            "readonly_superpower": schema("只读工具，忽略模式即可修改", {}, []),
            "custom_index": schema("查询索引", {}, []),
        },
        {"readonly_superpower": lambda a, r: {}, "custom_index": lambda a, r: {}},
        capabilities={"custom_index": ToolCapability.READ_ONLY},
        groups={"readonly_superpower": "files", "custom_index": "team"},
    )
    visibility = ToolVisibility(registry.directory(), ExecutionMode.PLAN)
    names = {entry.name for entry in visibility.listings()}
    assert {"read_file", "git_status", "custom_index"} <= names
    assert not {"run_command", "replace_text", "readonly_superpower"} & names
    visibility.discover(groups=("files", "team"))
    assert "readonly_superpower" not in visibility.snapshot().names
    with pytest.raises(ToolError):
        visibility.discover(names=("readonly_superpower",))


def test_schema_snapshots_and_new_turns_do_not_share_mutable_selection():
    spec = schema("中文描述 " + "规则\\路径\n" * 50, {"path": ("string", "参数说明")}, ["path"])
    registry = ToolRegistry(
        {"probe": spec},
        {"probe": lambda a, r: {}},
        ToolLimits(),
        capabilities={"probe": ToolCapability.READ_ONLY},
        groups={"probe": "files"},
    )
    directory = registry.directory()
    active = ToolVisibility(directory, ExecutionMode.ACT)
    active.discover(names=("probe",))
    selected = active.snapshot()
    assert len(active.listings()[0].summary) == 160
    assert "\n" not in active.listings()[0].summary
    definitions = selected.definitions()
    assert definitions[0]["function"]["description"] == spec["description"]
    assert selected.schema_bytes == len(json.dumps(definitions, ensure_ascii=False).encode("utf-8"))
    definitions[0]["function"]["parameters"]["required"].clear()
    spec["description"] = "externally changed"
    spec["parameters"]["required"].clear()
    assert selected.definitions()[0]["function"]["parameters"]["required"] == ["path"]
    assert selected.definitions()[0]["function"]["description"] != spec["description"]
    # 预览和下一轮都用新选择器；查看它们不会清空正在执行轮次的选择。
    for _ in range(2):
        preview = ToolVisibility(directory, ExecutionMode.ACT)
        assert preview.snapshot().names == ()
        assert active.snapshot() == selected
    newer = ToolVisibility(registry.directory(), ExecutionMode.ACT, ToolLoadingMode.FULL)
    assert newer.snapshot().definitions()[0]["function"]["description"] == "externally changed"


def test_full_and_on_demand_have_same_filtered_catalog_and_stable_registration_order(tmp_path):
    directory = create_tool_registry(tmp_path).directory()
    full = ToolVisibility(directory, ExecutionMode.PLAN, ToolLoadingMode.FULL)
    lazy = ToolVisibility(directory, ExecutionMode.PLAN)
    assert lazy.listings() == full.listings()
    assert lazy.snapshot().schema_bytes == 2
    names = full.snapshot().names
    lazy.discover(names=tuple(reversed(names)))
    assert lazy.snapshot() == full.snapshot()
    assert not {"replace_text", "run_command"} & set(names)


def test_discovery_does_not_grant_outside_read_or_bypass_edit_approval(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    target = root / "target.txt"
    target.write_text("before", encoding="utf-8")
    approvals = []
    registry = create_tool_registry(root, confirm_edit=lambda plan: approvals.append(plan) or False)
    selected = ToolVisibility(registry.directory(), ExecutionMode.ACT)
    selected.discover(groups=("files", "editing"))
    exposed = selected.snapshot()
    exposed.require("read_file")
    denied = json.loads(registry.execute("read_file", json.dumps({"path": str(outside)})))
    assert not denied["ok"] and approvals == []
    read = json.loads(registry.execute("read_file", '{"path":"target.txt"}'))
    exposed.require("replace_text")
    result = json.loads(
        registry.execute(
            "replace_text",
            json.dumps(
                {
                    "path": "target.txt",
                    "old_text": "before",
                    "new_text": "after",
                    "expected_version": read["version"],
                }
            ),
        )
    )
    assert result["status"] == "denied" and len(approvals) == 1
    assert target.read_text(encoding="utf-8") == "before"
    assert outside.read_text(encoding="utf-8") == "private"


def test_wrapper_extension_preserves_groups_and_current_mode_rechecks_old_snapshot(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    mode = ExecutionMode.ACT
    guard, approvals, extension_runs = [], [], []
    base = create_tool_registry(tmp_path, confirm_edit=lambda plan: approvals.append(plan) or True)
    registry = (
        base.guarded(lambda paths: guard.append(paths))
        .with_mode(lambda: mode)
        .extended(
            {"extra": schema("project query", {}, [])},
            {"extra": lambda a, r: extension_runs.append(a) or {}},
            capabilities={"extra": ToolCapability.READ_ONLY},
            groups={"extra": "team"},
        )
    )
    selected = ToolVisibility(registry.directory(), mode, ToolLoadingMode.FULL)
    groups = {entry.name: entry.group for entry in selected.listings()}
    assert groups["read_file"] == "files" and groups["replace_text"] == "editing"
    assert groups["extra"] == "team"
    assert "extra" not in {entry.name for entry in base.directory().listings(mode)}
    assert guard == approvals == extension_runs == []
    read = json.loads(registry.execute("read_file", '{"path":"target.txt"}'))
    mode = ExecutionMode.PLAN
    selected.snapshot().require("replace_text")
    result = json.loads(
        registry.execute(
            "replace_text",
            json.dumps(
                {
                    "path": "target.txt",
                    "old_text": "before",
                    "new_text": "after",
                    "expected_version": read["version"],
                }
            ),
        )
    )
    assert result["status"] == "mode_blocked"
    assert len(guard) == 1 and approvals == [] and target.read_text(encoding="utf-8") == "before"
    assert json.loads(registry.execute("extra", "{}"))["ok"]
    assert len(guard) == 2 and extension_runs == [{}]


def test_discovered_delegated_edit_still_returns_to_parent_without_approval(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    approvals = []
    registry = create_tool_registry(
        tmp_path,
        delegated=True,
        confirm_edit=lambda plan: approvals.append(plan) or True,
    )
    selected = ToolVisibility(registry.directory(), ExecutionMode.ACT)
    selected.discover(names=("replace_text",))
    selected.snapshot().require("replace_text")
    result = json.loads(
        registry.execute(
            "replace_text",
            json.dumps(
                {
                    "path": "target.txt",
                    "old_text": "before",
                    "new_text": "after",
                    "expected_version": "0" * 64,
                }
            ),
        )
    )
    assert not result["ok"] and result["status"] == "approval_required"
    assert approvals == [] and target.read_text(encoding="utf-8") == "before"

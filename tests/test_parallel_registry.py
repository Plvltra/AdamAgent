"""T6.2 注册策略：可信并发入口保持权限、来源包装及独立预检。"""

import json

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolHookError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.tool_calls.catalog import schema
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.registry import ToolRegistry


@pytest.mark.parametrize("capability", list(ToolCapability))
def test_parallel_handler_requires_read_only_capability(capability):
    kwargs = {
        "capabilities": {"probe": capability},
        "parallel_handlers": {"probe": lambda a, c: {}},
    }
    if capability is ToolCapability.READ_ONLY:
        assert ToolRegistry(
            {"probe": schema("probe", {}, [])}, {"probe": lambda a, r: {}}, ToolLimits(), **kwargs
        ).can_parallelize("probe")
    else:
        with pytest.raises(ValueError):
            ToolRegistry(
                {"probe": schema("probe", {}, [])},
                {"probe": lambda a, r: {}},
                ToolLimits(),
                **kwargs,
            )


def test_extensions_cannot_replace_existing_parallel_declarations(tmp_path):
    registry = create_tool_registry(tmp_path)
    with pytest.raises(ValueError):
        registry.extended(
            {"new": schema("new", {}, [])},
            {"new": lambda a, r: {}},
            parallel_handlers={"read_file": lambda a, c: {"wrong": True}},
        )
    assert registry.can_parallelize("read_file") and not registry.can_parallelize("run_command")


def test_wrappers_preserve_parallel_metadata_and_check_before_action(tmp_path):
    (tmp_path / "note.txt").write_text("evidence")
    guarded = []
    registry = create_tool_registry(tmp_path)
    registry = (
        registry.extended(
            {"stateful": schema("cached query", {}, [])},
            {"stateful": lambda a, r: {}},
            capabilities={"stateful": ToolCapability.READ_ONLY},
        )
        .guarded(lambda paths: guarded.append(paths))
        .with_mode(lambda: ExecutionMode.PLAN)
    )
    assert registry.can_parallelize("read_file") and not registry.can_parallelize("stateful")
    assert not registry.can_parallelize("unknown")
    stages = []

    def before(name, args, mode):
        stages.append("before")
        assert mode is ExecutionMode.PLAN
        assert len(guarded) == 1
        raise ToolHookError("blocked by local checker")

    action = registry.prepare("read_file", '{"path":"note.txt"}', before_tool=before)
    assert stages == ["before"]
    output = action(CancellationToken())
    assert output["status"] == "hook_blocked"
    assert json.loads(registry.encode("read_file", output))["ok"] is False

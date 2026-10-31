"""T12.1 可信目录边界：错误分组不能改写已有元数据，无效说明不能进入请求快照。"""

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery import ToolDirectory, ToolVisibility
from adam.tool_calls.registry import ToolRegistry


@pytest.mark.parametrize(
    "groups",
    [
        {"missing": "files"},
        {"probe": ""},
        {"probe": "a" * 65},
        {"probe": "../files"},
        {"probe": False},
    ],
)
def test_invalid_registration_groups_are_rejected(groups):
    with pytest.raises(ValueError):
        ToolRegistry(
            {"probe": schema("probe", {}, [])},
            {"probe": lambda a, r: {}},
            ToolLimits(),
            groups=groups,
        )


def test_extension_cannot_relabel_existing_tool_and_default_is_extensions():
    base = ToolRegistry(
        {"probe": schema("probe", {}, [])},
        {"probe": lambda a, r: {}},
        ToolLimits(),
        groups={"probe": "files"},
    )
    with pytest.raises(ValueError):
        base.extended(
            {"extra": schema("extra", {}, [])},
            {"extra": lambda a, r: {}},
            groups={"probe": "editing"},
        )
    extended = base.extended({"extra": schema("extra", {}, [])}, {"extra": lambda a, r: {}})
    entries = extended.directory().listings(ExecutionMode.ACT)
    assert [(entry.name, entry.group) for entry in entries] == [
        ("probe", "files"),
        ("extra", "extensions"),
    ]
    assert extended.directory().listings(ExecutionMode.PLAN) == ()


@pytest.mark.parametrize(
    "specs",
    [
        {"invalid name": {}},
        {"probe": {"name": "spoof"}},
        {"probe": {"description": False}},
        {"probe": {"value": float("nan")}},
        {"probe": {"value": object()}},
        {"probe": {"description": "\ud800"}},
    ],
)
def test_invalid_metadata_cannot_produce_model_schema(specs):
    with pytest.raises(ValueError):
        ToolDirectory(specs, {})


def test_directory_rejects_invalid_capability_and_mode_even_when_empty():
    with pytest.raises(ValueError):
        ToolDirectory({"probe": {}}, {"probe": "read_only"})
    with pytest.raises(ValueError):
        ToolDirectory({}, {"missing": ToolCapability.READ_ONLY})
    directory = ToolDirectory({}, {})
    with pytest.raises(ValueError):
        directory.listings("plan")
    with pytest.raises(ValueError):
        ToolVisibility(directory, ExecutionMode.ACT, "on_demand")

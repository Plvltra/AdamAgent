"""模式契约的类别组合；工具副作用、审批和日志由 features/test_mode_policy.py 验证。"""

import pytest

from adam.contracts.errors import ToolModeError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode


@pytest.mark.parametrize("capability", list(ToolCapability))
def test_act_leaves_capability_decision_to_existing_tool_authorization(capability):
    check_tool_mode(ExecutionMode.ACT, capability)


@pytest.mark.parametrize(
    "capability",
    [ToolCapability.READ_ONLY, ToolCapability.SESSION_STATE, ToolCapability.DELEGATE],
)
def test_plan_accepts_investigation_internal_records_and_restricted_delegation(capability):
    check_tool_mode(ExecutionMode.PLAN, capability)


@pytest.mark.parametrize(
    "capability",
    [ToolCapability.EDIT_FILE, ToolCapability.RUN_PROCESS, ToolCapability.UNKNOWN],
)
def test_plan_rejects_workspace_changes_processes_and_unknown_extensions(capability):
    with pytest.raises(ToolModeError, match="/mode act"):
        check_tool_mode(ExecutionMode.PLAN, capability)


@pytest.mark.parametrize(
    ("mode", "capability"),
    [("act", ToolCapability.READ_ONLY), (ExecutionMode.ACT, "read_only"), (None, None)],
)
def test_unvalidated_mode_or_capability_is_not_treated_as_permission(mode, capability):
    with pytest.raises(ValueError):
        check_tool_mode(mode, capability)

"""运行模式与工具能力声明；来自可信注册代码，不从模型参数或工具描述推断权限。"""

from enum import StrEnum

from adam.contracts.errors import ToolModeError


class ExecutionMode(StrEnum):
    """终端本次运行的模式；act 仍须遵循工具原有路径与审批规则。"""

    PLAN = "plan"
    ACT = "act"


class ToolCapability(StrEnum):
    """工具可能产生的动作；会话内部记录与工作区修改分别声明。"""

    READ_ONLY = "read_only"
    SESSION_STATE = "session_state"
    DELEGATE = "delegate"
    EDIT_FILE = "edit_file"
    RUN_PROCESS = "run_process"
    UNKNOWN = "unknown"


def check_tool_mode(mode: ExecutionMode, capability: ToolCapability) -> None:
    """模式禁止时抛出受控工具错误；调用者须在路径预检、确认和实际动作之前调用。

    DELEGATE 仅用于由程序约束的子任务入口，组装方必须让子任务继承模式。
    未知扩展在 plan 中拒绝；act 只解除这一层限制，不代替已有授权。
    """
    if not isinstance(mode, ExecutionMode) or not isinstance(capability, ToolCapability):
        raise ValueError("模式与工具能力必须使用已校验的枚举。")
    if mode is ExecutionMode.PLAN and capability not in {
        ToolCapability.READ_ONLY,
        ToolCapability.SESSION_STATE,
        ToolCapability.DELEGATE,
    }:
        raise ToolModeError(
            "当前为 plan 模式，未执行此动作；仅允许调查与内部记录。"
            "需要执行修改时，由用户使用 /mode act 切换，之后仍须原有批准。"
        )

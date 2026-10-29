"""组装内置工具；扩展工具时在此注册说明、处理函数和所需的结果编码器。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.models import CommandRequest, EditPlan, ExecutionRecorder
from adam.tool_calls.catalog import SPECS
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.file_tools import FileTools
from adam.tool_calls.git_tools import GitTools
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.registry import ToolHandler, ToolRegistry


def create_tool_registry(
    workspace: Path,
    permissions: PermissionConfig | None = None,
    limits: ToolLimits | None = None,
    *,
    confirm_command: Callable[[CommandRequest], bool] | None = None,
    confirm_edit: Callable[[EditPlan], bool] | None = None,
    delegated: bool = False,
    protected_dirs: tuple[Path, ...] = (),
) -> ToolRegistry:
    """创建内置工具；工作目录无效时失败，未提供确认回调时命令及编辑默认拒绝。

    protected_dirs 是程序保留目录，即使位于已授权根内，文件及 Git diff 工具也拒绝读取。
    已批准命令不受路径策略约束；子任务的命令入口始终拒绝执行。
    """
    policy = PathPolicy(workspace, permissions or PermissionConfig(), protected_dirs=protected_dirs)
    limits = limits or ToolLimits()
    files = FileTools(policy, limits)
    git = GitTools(policy, limits)
    commands = CommandTools(policy.workspace, limits, confirm_command)
    edits = EditTools(policy, limits, confirm_edit)
    readers = {
        "file_info": files.file_info,
        "list_directory": files.list_directory,
        "read_file": files.read_file,
        "find_files": files.find_files,
        "search_text": files.search_text,
        "git_status": git.git_status,
        "git_diff": git.git_diff,
        "git_log": git.git_log,
    }
    handlers = {name: _path_handler(policy, handler) for name, handler in readers.items()}
    # 命令批准后的进程不受只读路径规则限制；只读工具在适配器中检查路径。
    handlers["run_command"] = commands.run_command
    handlers["replace_text"] = edits.replace_text
    if delegated:
        # 不调用审批回调，也不构造修改计划或启动程序；将需要人工授权的工作交回主 Agent。
        handlers["run_command"] = _needs_parent
        handlers["replace_text"] = _needs_parent
    return ToolRegistry(
        SPECS, handlers, limits, {} if delegated else {"run_command": commands.encode_result}
    )


def _needs_parent(args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
    """子 Agent 不能申请新权限；模型仍可解释限制并完成辅助分析。"""
    return {
        "ok": False,
        "status": "approval_required",
        "error": "此动作需要额外批准，子 Agent 不能执行；请向主 Agent 提供方案，由其接手。",
    }


def _path_handler(
    policy: PathPolicy, handler: Callable[[Path, dict[str, Any]], dict[str, Any]]
) -> ToolHandler:
    """将只读工具接到统一调用接口，在任何实际读取前执行路径授权。"""

    def execute(args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        return handler(policy.resolve(args["path"]), args)

    return execute

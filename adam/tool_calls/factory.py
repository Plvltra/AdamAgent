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
) -> ToolRegistry:
    """创建内置工具目录；工作目录无效时失败，未提供确认回调时命令及编辑默认拒绝。"""
    policy = PathPolicy(workspace, permissions or PermissionConfig())
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
    return ToolRegistry(SPECS, handlers, limits, {"run_command": commands.encode_result})


def _path_handler(
    policy: PathPolicy, handler: Callable[[Path, dict[str, Any]], dict[str, Any]]
) -> ToolHandler:
    """将只读工具接到统一调用接口，在任何实际读取前执行路径授权。"""

    def execute(args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        return handler(policy.resolve(args["path"]), args)

    return execute

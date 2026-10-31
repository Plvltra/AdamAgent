"""组装内置工具；扩展工具时在此注册说明、处理函数和所需的结果编码器。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.execution import ToolCapability
from adam.contracts.models import CommandRequest, EditPlan, ExecutionRecorder
from adam.tool_calls.catalog import SPECS
from adam.tool_calls.code_tools import CodeTools, encode_result
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.file_tools import FileTools
from adam.tool_calls.git_tools import GitTools
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.registry import ParallelHandler, ToolHandler, ToolRegistry

PathHandler = Callable[[Path, dict[str, Any]], dict[str, Any]]


def create_tool_registry(
    workspace: Path,
    permissions: PermissionConfig | None = None,
    limits: ToolLimits | None = None,
    *,
    confirm_command: Callable[[CommandRequest], bool] | None = None,
    confirm_edit: Callable[[EditPlan], bool] | None = None,
    delegated: bool = False,
    protected_dirs: tuple[Path, ...] = (),
    command_output_bytes: int | None = None,
) -> ToolRegistry:
    """创建内置工具；工作目录无效时失败，未提供确认回调时命令及编辑默认拒绝。

    protected_dirs 是程序保留目录，即使位于已授权根内，文件及 Git diff 工具也拒绝读取。
    已批准命令不受路径策略约束；子任务的命令入口始终拒绝执行。
    command_output_bytes 只控制管道捕获，不改变单次回传上限；附件在外层保存。
    """
    policy = PathPolicy(workspace, permissions or PermissionConfig(), protected_dirs=protected_dirs)
    limits = limits or ToolLimits()
    commands = CommandTools(
        policy.workspace, limits, confirm_command, capture_bytes=command_output_bytes
    )
    edits = EditTools(policy, limits, confirm_edit)
    readers = _readers(policy, limits)
    handlers = {name: _path_handler(policy, handler) for name, handler in readers.items()}
    # 命令批准后的进程不受只读路径规则限制；只读工具在适配器中检查路径。
    handlers["run_command"] = commands.run_command
    handlers["replace_text"] = edits.replace_text
    if delegated:
        # 不调用审批回调，也不构造修改计划或启动程序；将需要人工授权的工作交回主 Agent。
        handlers["run_command"] = _needs_parent
        handlers["replace_text"] = _needs_parent
    return ToolRegistry(
        SPECS,
        handlers,
        limits,
        {
            **dict.fromkeys(
                ("code_structure", "find_definitions", "find_references"), encode_result
            ),
            **({} if delegated else {"run_command": commands.encode_result}),
        },
        parallel_handlers={name: _parallel_path_handler(policy, limits, name) for name in readers},
        capabilities={
            **dict.fromkeys(readers, ToolCapability.READ_ONLY),
            "replace_text": ToolCapability.EDIT_FILE,
            "run_command": ToolCapability.RUN_PROCESS,
        },
        path_selectors={
            **{name: lambda args: (Path(args["path"]),) for name in readers},
            # 子任务的写入/命令始终交回主任务，不为不会执行的动作发现额外路径。
            **(
                {}
                if delegated
                else {
                    "replace_text": lambda args: (Path(args["path"]),),
                    "run_command": lambda args: (Path(args.get("cwd", ".")),),
                }
            ),
        },
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


def _readers(
    policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
) -> dict[str, PathHandler]:
    """生成无共享调用状态的文件、代码和 Git 查询，取消令牌只属于本次动作。"""
    files = FileTools(policy, limits, cancel)
    code = CodeTools(policy, limits, cancel)
    git = GitTools(policy, limits, cancel)
    return {
        "code_structure": code.code_structure,
        "find_definitions": code.find_definitions,
        "find_references": code.find_references,
        "file_info": files.file_info,
        "list_directory": files.list_directory,
        "read_file": files.read_file,
        "find_files": files.find_files,
        "search_text": files.search_text,
        "git_status": git.git_status,
        "git_diff": git.git_diff,
        "git_log": git.git_log,
    }


def _parallel_path_handler(policy: PathPolicy, limits: ToolLimits, name: str) -> ParallelHandler:
    """适配可信读取到取消接口；不接收或捕获会话日志及界面回调。"""

    def execute(args: dict[str, Any], cancel: CancellationToken) -> dict[str, Any]:
        cancel.check()
        path = policy.resolve(args["path"])
        cancel.check()
        return _readers(policy, limits, cancel)[name](path, args)

    return execute

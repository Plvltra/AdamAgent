"""工具声明、参数校验、分发与结果封装。"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.tool_calls.command_tools import CommandRequest, CommandTools, ExecutionRecorder
from adam.tool_calls.file_tools import FileTools
from adam.tool_calls.git_tools import GitTools
from adam.tool_calls.policy import PathPolicy, PermissionConfig, ToolError, ToolLimits


def schema(
    description: str, fields: dict[str, tuple[str, str]], required: list[str]
) -> dict[str, Any]:
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                key: {"type": kind, "description": detail} for key, (kind, detail) in fields.items()
            },
            "required": required,
            "additionalProperties": False,
        },
    }


PATH = ("string", "文件或目录路径；相对路径以工作目录为基准，绝对路径须已授权")
SPECS = {
    "run_command": {
        "description": "执行通用程序及参数，需用户逐次确认。支持脚本、Git、构建等命令；"
        "不隐式解释 shell 语法。读取 exit_code/stdout/stderr 判断结果，拒绝后不要重复请求。",
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "程序及参数数组，例如 [git, status, --short]；脚本用解释器执行",
                },
                "cwd": {
                    "type": "string",
                    "description": "执行目录，默认启动工作目录；相对该目录解析",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "可选超时秒数，不超过用户配置的命令超时上限",
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
    "list_directory": schema(
        "列出直接子项，默认200项；offset可翻页。",
        {"path": PATH, "offset": ("integer", "从0开始")},
        ["path"],
    ),
    "find_files": schema(
        "递归按glob查找文件，跳过依赖、缓存、.git；结果最多200条。",
        {"path": PATH, "pattern": ("string", "如 **/*.py")},
        ["path", "pattern"],
    ),
    "read_file": schema(
        "按行读取UTF-8文件，返回完整文件version与行号；超长行截断，可用start_line继续。",
        {
            "path": PATH,
            "start_line": ("integer", "从1开始，默认1"),
            "max_lines": ("integer", "最多200行，默认200"),
        },
        ["path"],
    ),
    "search_text": schema(
        "递归搜索字面文本，返回路径、行号和匹配行，最多50条。",
        {
            "path": PATH,
            "query": ("string", "非空字面文本"),
            "pattern": ("string", "文件glob，默认*"),
        },
        ["path", "query"],
    ),
    "file_info": schema("查询文件类型、字节大小与修改时间。", {"path": PATH}, ["path"]),
    "git_status": schema("只读查询Git状态，不运行用户命令。", {"path": PATH}, ["path"]),
    "git_diff": schema(
        "只读查看工作区或暂存区diff；禁用外部diff与textconv。",
        {"path": PATH, "staged": ("boolean", "默认false")},
        ["path"],
    ),
    "git_log": schema(
        "只读查看最近提交摘要，最多50条。", {"path": PATH, "limit": ("integer", "默认10")}, ["path"]
    ),
}


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        permissions: PermissionConfig | None = None,
        limits: ToolLimits | None = None,
        *,
        confirm_command: Callable[[CommandRequest], bool] | None = None,
    ) -> None:
        self.policy = PathPolicy(workspace, permissions or PermissionConfig())
        self.limits = limits or ToolLimits()
        self.commands = CommandTools(self.policy, self.limits, confirm_command)
        files = FileTools(self.policy, self.limits)
        git = GitTools(self.policy, self.limits)
        self._handlers: dict[str, Callable[[Path, dict[str, Any]], dict[str, Any]]] = {
            "file_info": files.file_info,
            "list_directory": files.list_directory,
            "read_file": files.read_file,
            "find_files": files.find_files,
            "search_text": files.search_text,
            "git_status": git.git_status,
            "git_diff": git.git_diff,
            "git_log": git.git_log,
        }

    def definitions(self) -> list[dict[str, Any]]:
        """返回供 Chat Completions tools 字段使用的工具说明。"""
        return [
            {"type": "function", "function": {"name": name, **spec}} for name, spec in SPECS.items()
        ]

    def execute(
        self, name: str, arguments: str, *, record_execution: ExecutionRecorder | None = None
    ) -> str:
        """校验 JSON 参数并执行工具，返回有界 JSON 字符串；工具错误作为结果回传。

        record_execution 接收命令状态事件，普通只读工具忽略该参数。
        CommandLogError 不转换为普通工具结果，调用方应停止本轮以免自动重试。
        """
        try:
            if name not in SPECS:
                raise ToolError("未知工具。")
            args = json.loads(arguments)
            params = SPECS[name]["parameters"]
            if not isinstance(args, dict) or args.keys() - params["properties"].keys():
                raise ToolError("参数必须为对象且不能包含未知字段。")
            if any(key not in args for key in params["required"]):
                raise ToolError("缺少必填参数。")
            types = {"string": str, "integer": int, "boolean": bool, "array": list}
            for key, value in args.items():
                if type(value) is not types[params["properties"][key]["type"]]:
                    raise ToolError(f"参数 {key} 类型错误。")
            result = (
                self.commands.run_command(args, record_execution)
                if name == "run_command"
                else self._handlers[name](self.policy.resolve(args["path"]), args)
            )
            output = {"ok": True, **result}
        except (ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
            # 操作系统异常可能包含敏感路径；只回传已控制的业务提示。
            output = {
                "ok": False,
                "error": str(error)
                if isinstance(error, ToolError)
                else "工具执行失败：文件编码、访问权限、Git或参数格式无效。",
            }
        if name == "run_command" and "status" in output:
            return self._encode_command(output)
        encoded = json.dumps(output, ensure_ascii=False)
        if len(encoded) > self.limits.max_result_chars:
            # 保证截断后仍为合法JSON；预览不再作为原始结构解析。
            preview = encoded[: max(0, self.limits.max_result_chars // 3)]
            encoded = json.dumps(
                {
                    "ok": output["ok"],
                    **({"version": output["version"]} if "version" in output else {}),
                    "truncated": True,
                    "preview": preview,
                    "hint": "请缩小范围或分段读取。",
                },
                ensure_ascii=False,
            )
        return encoded

    def _encode_command(self, output: dict[str, Any]) -> str:
        """裁剪命令输出时保留退出码和状态，避免模型误把截断预览当执行结果。"""
        encoded = json.dumps(output, ensure_ascii=False)
        while len(encoded) > self.limits.max_result_chars:
            key = "stdout" if len(output["stdout"]) >= len(output["stderr"]) else "stderr"
            output[key] = output[key][: len(output[key]) // 2]
            output["truncated"] = True
            encoded = json.dumps(output, ensure_ascii=False)
        return encoded

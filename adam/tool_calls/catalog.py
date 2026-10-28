"""内置工具的模型可见说明；与执行和对象创建分开维护。"""

from typing import Any


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
    "replace_text": schema(
        "修改已有UTF-8文件的一处精确匹配。先read_file取得version，用户确认diff后才写入；拒绝后停止。",
        {
            "path": PATH,
            "old_text": ("string", "唯一匹配的旧文本，不能为空"),
            "new_text": ("string", "替换文本，可为空"),
            "expected_version": ("string", "read_file返回的完整文件version，必须原样传回"),
        },
        ["path", "old_text", "new_text", "expected_version"],
    ),
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

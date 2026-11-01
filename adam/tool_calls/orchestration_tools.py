"""模型代码编排入口；仅依赖隔离执行契约，底层工具交回原 ToolExecutor。"""

from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.contracts.execution import CODE_TOOL_NAME, ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.contracts.orchestration import CodeRunner, ToolCallback
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

SPEC = schema(
    "在固定隔离环境执行 Python 3.8 兼容代码，可循环、筛选和聚合。"
    "用 call_tool(name, arguments) 串行调用本次请求已提供完整说明的工具，"
    "将 JSON 可编码的最终摘要赋给 result；中间结果留在 worker。"
    "原权限、Hook、批准和额度仍有效；不能递归 execute_code 或通过发现扩展本次名单。"
    "最多16次底层调用，主轮额度还会计入本入口一次。"
    "无直接宿主文件、网络或进程权限，CPU2秒/计算5秒/256MiB；不用于运行安装命令。"
    "只在需要组合处理多个工具结果时使用；结果失败不证明底层已发生动作被撤销。",
    {"code": ("string", "1～65536 UTF-8 字节的 Python 代码；设置 result，不把完整原文逐项回传")},
    ["code"],
)


def register_orchestration(
    registry: ToolRegistry,
    runner: CodeRunner,
    invoke: ToolCallback,
    *,
    cancel: CancellationToken | None = None,
) -> ToolRegistry:
    """注册串行编排入口，不探测或启动后端；invoke 必须连接同一个执行器的嵌套入口。"""

    def run(args: dict[str, Any], _record: ExecutionRecorder | None) -> dict[str, Any]:
        """已知退出返回事实；隔离/日志/取消故障保持异常，停止本轮而不隐式重跑。"""
        result = runner.run(args["code"], invoke, cancel=cancel)
        succeeded = result.has_result and result.exit_code == 0 and result.reason == "exited"
        return {
            "ok": succeeded,
            "status": "completed" if succeeded else "failed",
            "result": result.result,
            "has_result": result.has_result,
            "exit_code": result.exit_code,
            "reason": result.reason,
            "tool_calls": result.calls,
            "stderr": result.stderr,
        }

    return registry.extended(
        {CODE_TOOL_NAME: SPEC},
        {CODE_TOOL_NAME: run},
        validators={CODE_TOOL_NAME: _validate},
        capabilities={CODE_TOOL_NAME: ToolCapability.DELEGATE},
        groups={CODE_TOOL_NAME: "orchestration"},
    )


def _validate(value: object) -> None:
    """在运行 Hook 与隔离探测前拒绝超长或不可编码的代码，不回显程序正文。"""
    if (
        not isinstance(value, dict)
        or value.keys() != {"code"}
        or not isinstance(value["code"], str)
    ):
        raise ToolError("编排参数必须只包含 code 文本。")
    try:
        size = len(value["code"].encode("utf-8"))
    except UnicodeError as error:
        raise ToolError("编排代码无法编码为 UTF-8。") from error
    if not 1 <= size <= 65536:
        raise ToolError("编排代码须为 1～65536 UTF-8 字节。")

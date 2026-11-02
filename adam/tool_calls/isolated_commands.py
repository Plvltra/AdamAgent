"""显式隔离命令入口：批准有版本的输入快照，执行端由组装层注入。"""

import json
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.command_execution import CommandInputPreview, CommandRunner
from adam.contracts.command_inputs import validate_argv, validate_names
from adam.contracts.errors import CommandLogError, ToolError
from adam.contracts.execution import ISOLATED_COMMAND_TOOL, ToolCapability
from adam.contracts.models import CommandRequest, ExecutionRecorder
from adam.tool_calls.command_inputs import capture_inputs
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.registry import ToolRegistry
from adam.tool_calls.result_preview import encode_head_tail

SPEC = {
    "description": "在 WSL Linux 隔离环境中运行单个原生程序，逐次批准。"
    "只读 /work 仅包含 inputs 明确选择并获原路径授权的文件快照，不挂载项目。"
    "无网络、禁止派生子进程，CPU2秒/256MiB/合计输出64KiB；不适合安装、构建或改文件。"
    "使用 Linux 程序和路径，不拼接 shell；读取 status/reason/exit_code 判断结果。"
    "后端不可用会停止，不自动改用宿主 run_command。",
    "parameters": {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 128,
                "description": "Linux 程序及参数，例如 [cat, src/main.py]；工作目录固定 /work",
            },
            "inputs": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "maxProperties": 16,
                "description": "虚拟相对文件名到宿主源文件路径；最多16个、单个1MiB、合计2MiB",
            },
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["argv"],
        "additionalProperties": False,
    },
}


def register_isolated_commands(
    registry: ToolRegistry,
    runner: CommandRunner,
    policy: PathPolicy,
    distribution: str,
    *,
    confirm: Callable[[CommandRequest], bool] | None = None,
    cancel: CancellationToken | None = None,
) -> ToolRegistry:
    """注册主 Agent 的串行动作，不探测后端；原模式、Hook、预算和记录仍由执行器控制。

    子 Agent 不注册此入口，需把命令方案交回主 Agent。cancel 绑定本次运行的取消令牌；
    终端 KeyboardInterrupt 直接传至后端清理，不作为可重试工具失败。
    """
    tool = IsolatedCommandTool(runner, policy, registry.limits, distribution, confirm, cancel)
    return registry.extended(
        {ISOLATED_COMMAND_TOOL: SPEC},
        {ISOLATED_COMMAND_TOOL: tool.run},
        {ISOLATED_COMMAND_TOOL: tool.encode},
        {ISOLATED_COMMAND_TOOL: _validate},
        path_selectors={
            ISOLATED_COMMAND_TOOL: lambda args: tuple(
                Path(p) for p in args.get("inputs", {}).values()
            )
        },
        capabilities={ISOLATED_COMMAND_TOOL: ToolCapability.RUN_PROCESS},
        groups={ISOLATED_COMMAND_TOOL: "commands"},
    )


class IsolatedCommandTool:
    """只协调输入、批准和命令事实；不持有模型配置，不了解后端启动命令。"""

    def __init__(
        self,
        runner: CommandRunner,
        policy: PathPolicy,
        limits: ToolLimits,
        distribution: str,
        confirm: Callable[[CommandRequest], bool] | None,
        cancel: CancellationToken | None,
    ) -> None:
        self.runner, self.policy, self.limits = runner, policy, limits
        self.distribution, self.confirm, self.cancel = distribution, confirm, cancel

    def run(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """批准后复核源版本，先落开始事件再执行；隔离故障保持未知，禁止隐式宿主重试。"""
        token = self.cancel or CancellationToken()
        token.check()
        batch = capture_inputs(
            self.policy,
            args.get("inputs", {}),
            cancel=token,
        )
        command = CommandRequest(
            tuple(args["argv"]),
            PurePosixPath("/work"),
            args.get("timeout_seconds", 5),
            "isolated",
            self.distribution,
            tuple(CommandInputPreview(s.name, str(s.path), s.version) for s in batch.sources),
        )
        details = {
            "argv": list(command.argv),
            "cwd": "/work",
            "timeout_seconds": command.timeout_seconds,
            "environment": "isolated",
            "distribution": self.distribution,
            "input_files": [
                {"name": s.name, "source": str(s.path), "version": s.version} for s in batch.sources
            ],
        }
        if self.confirm is None or not self.confirm(command):
            result = self._empty("denied")
            self._record(record, "command_denied", {**details, **result})
            return result
        token.check()
        batch.verify(self.policy, token)
        self._record(record, "command_started", details)
        started = time.monotonic()
        try:
            actual = self.runner.run(
                command.argv,
                timeout_seconds=command.timeout_seconds,
                inputs=batch.files,
                cancel=token,
            )
        except (KeyboardInterrupt, EOFError):
            # 后端未交付可靠结果；不能把空输出、未知退出码解释为完整的执行事实。
            result = {
                **self._empty("interrupted"),
                "truncated": True,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            self._record(record, "command_finished", result)
            raise
        status = {
            "exited": "completed",
            "wall_limit": "timed_out",
            "output_limit": "output_limited",
        }[actual.reason]
        result = {
            **self._empty(status),
            "ok": status == "completed",
            "exit_code": actual.exit_code,
            "stdout": actual.stdout,
            "stderr": actual.stderr,
            "reason": actual.reason,
            "policy": actual.policy,
            "truncated": actual.reason != "exited" or actual.invalid_utf8,
            "invalid_utf8": actual.invalid_utf8,
            "elapsed_seconds": actual.duration_ms / 1000,
            "captured_bytes": actual.captured_bytes,
        }
        self._record(record, "command_finished", json.loads(self.encode(result)))
        return result

    def encode(self, output: dict[str, Any]) -> str:
        """只缩短模型预览，保留状态与隔离环境；原输出仍交给外层附件保存。"""
        return encode_head_tail(
            output,
            {"stdout": output.get("stdout", ""), "stderr": output.get("stderr", "")},
            self.limits.max_result_chars,
            optional=("elapsed_seconds", "captured_bytes"),
        )

    @staticmethod
    def _empty(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": status,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "environment": "isolated",
            "truncated": False,
        }

    @staticmethod
    def _record(record: ExecutionRecorder | None, kind: str, payload: dict[str, Any]) -> None:
        if record is not None:
            try:
                record(kind, payload)
            except OSError as error:
                raise CommandLogError(
                    "隔离命令日志保存失败；结果可能未知，已停止且不自动重试。"
                ) from error


def _validate(value: object) -> None:
    """在路径发现、Hook 和批准前校验完整输入；原路径权限仍在实际读取时检查。"""
    if not isinstance(value, dict) or value.keys() - {"argv", "inputs", "timeout_seconds"}:
        raise ToolError("隔离命令参数必须只包含 argv、inputs 和 timeout_seconds。")
    try:
        if not isinstance(value.get("argv"), list):
            raise ValueError("invalid argv")
        validate_argv(value["argv"])
        selected = value.get("inputs", {})
        if not isinstance(selected, dict) or any(
            not isinstance(path, str) or not path.strip() or "\0" in path
            for path in selected.values()
        ):
            raise ValueError("invalid input mapping")
        validate_names(tuple(selected))
        timeout = value.get("timeout_seconds", 5)
        if type(timeout) is not int or not 1 <= timeout <= 5:
            raise ValueError("invalid timeout")
    except (ValueError, UnicodeError) as error:
        raise ToolError(
            "隔离命令参数、输入名称或超时无效；未读取输入、申请批准或启动后端。"
        ) from error

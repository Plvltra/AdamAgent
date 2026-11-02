"""按用户配置运行本地 Hook，并把协议、进程故障转换为可记录的检查结果。"""

import time
from dataclasses import replace
from pathlib import Path

from adam.config.hooks import HooksConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.diagnostics import match_edit
from adam.contracts.hooks import HookEvent, HookRequest, HookResult, parse_response
from adam.hooks.process import run_process


class HookRunner:
    """只持有检查配置与工作目录，不接收模型客户端、密钥或工具注册表。"""

    def __init__(
        self,
        config: HooksConfig,
        workspace: Path,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.config, self.workspace, self.cancel = config, workspace.resolve(), cancel

    def configured(self, event: HookEvent) -> bool:
        """返回用户是否启用了此事件；查询本身不创建进程。"""
        if not isinstance(event, HookEvent):
            raise ValueError("invalid hook event")
        command = (
            self.config.before_tool if event is HookEvent.BEFORE_TOOL else self.config.after_tool
        )
        return self.config.enabled and command is not None

    def run(self, request: HookRequest) -> HookResult | None:
        """执行一个已配置事件；关闭或未配置时返回 None，且不创建进程。

        非法输入、进程失败与坏响应返回 failed，供前置流程阻止工具；
        after_tool 的失败不能回滚已发生动作。取消信号在清理后继续传播。
        正常交付的诊断单独记录文件状态，须对应本次已成功编辑的路径和版本。
        """
        if not self.configured(request.event):
            return None
        command = (
            self.config.before_tool
            if request.event is HookEvent.BEFORE_TOOL
            else self.config.after_tool
        )
        if command is None:
            return None
        started = time.monotonic()
        try:
            payload = request.encode(str(self.workspace), self.config.max_input_bytes)
        except ValueError:
            return HookResult("failed", "Hook 输入无效或超过上限。", "invalid_input")
        outcome = run_process(
            command.argv,
            self.workspace,
            payload,
            self.config.timeout_seconds,
            self.config.max_output_bytes,
            self.cancel,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        stderr = outcome.stderr.decode("utf-8", errors="replace")
        error = outcome.error_code or ("nonzero_exit" if outcome.exit_code != 0 else "")
        if not error:
            try:
                result = parse_response(request.event, outcome.stdout)
                if result.diagnostics is not None:
                    match_edit(result.diagnostics, request.tool_name, request.result)
            except (ValueError, TypeError, RecursionError):
                error = "invalid_response"
            else:
                return replace(result, duration_ms=elapsed, stderr=stderr)
        return HookResult(
            "failed",
            "本地 Hook 未可靠完成，不能将其视为检查通过。",
            error,
            outcome.exit_code,
            elapsed,
            stderr,
        )

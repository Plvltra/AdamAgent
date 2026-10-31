"""按真实工具结果识别连续重复失败；不执行工具、不写日志，也不决定权限。"""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

from adam.config.settings import NoProgressConfig


class ProgressAction(StrEnum):
    """调用方应采取的动作；stop 后不得继续派发本轮工具或模型请求。"""

    CONTINUE = "continue"
    WARN = "warn"
    STOP = "stop"


@dataclass(frozen=True)
class ProgressDecision:
    """一次观察后的不可变结论；指纹只用于关联，不能反推出权限或完整参数。"""

    action: ProgressAction = ProgressAction.CONTINUE
    consecutive_failures: int = 0
    fingerprint: str | None = None
    category: str | None = None


class NoProgressGuard:
    """一个轮次一个实例，仅保存最近的失败指纹；主任务与子任务不能共享实例。"""

    def __init__(self, config: NoProgressConfig | None = None) -> None:
        self._config = config or NoProgressConfig()
        self._decision = ProgressDecision()

    def observe(
        self,
        name: str,
        arguments: str,
        result: dict[str, Any],
        *,
        evidence_version: str | None = None,
    ) -> ProgressDecision:
        """观察尚未截断的实际结果，返回继续、首次提醒或停止，不重新执行动作。

        Args:
            arguments: 模型原始 JSON 参数；对象键顺序与空白不影响指纹，非法 JSON
                则按去除首尾空白后的原文区分。调用 ID 不参与判断。
            result: 工具实际返回值；命令正常结束但 exit_code 非零仍算失败。
            evidence_version: 宿主核实的外部状态版本；不可直接采信模型声称的版本。
                版本变化只重置计数，不授予权限，也不表示任务已经成功。

        同名、同参数、同失败类别及同版本连续出现才累计；成功、非失败状态或不同
        指纹重新计数。停止结论锁定到本实例结束。此策略不判断任务语义是否完成。
        """
        if not self._config.enabled:
            return self._decision
        if self._decision.action is ProgressAction.STOP:
            return self._decision
        category = _failure_category(result)
        if category is None:
            self._decision = ProgressDecision()
            return self._decision
        fingerprint = _fingerprint(name, arguments, category, evidence_version)
        count = (
            self._decision.consecutive_failures + 1
            if fingerprint == self._decision.fingerprint
            else 1
        )
        if count >= self._config.stop_after:
            action = ProgressAction.STOP
        elif count == self._config.warn_after:
            action = ProgressAction.WARN
        else:
            action = ProgressAction.CONTINUE
        self._decision = ProgressDecision(action, count, fingerprint, category)
        return self._decision


def _failure_category(result: dict[str, Any]) -> str | None:
    """策略拒绝、待处理和取消由原流程处理；只判断工具业务失败与命令非零退出。"""
    status = result.get("status")
    if status in (
        "deferred",
        "mode_blocked",
        "hook_blocked",
        "denied",
        "approval_required",
        "pending",
        "running",
        "interrupted",
        "cancelled",
    ):
        return None
    exit_code = result.get("exit_code")
    if type(exit_code) is int and exit_code != 0:
        return f"command_exit:{exit_code}"
    if result.get("ok") is not False:
        return None
    if isinstance(status, str) and status not in {"", "completed"}:
        return f"tool_status:{status}"
    # 原工具错误没有统一机器码，以受控错误说明区分原因，避免把修复后的新错误合并。
    error = result.get("error")
    detail = " ".join(error.split()) if isinstance(error, str) else "unspecified"
    encoded = json.dumps(detail, ensure_ascii=True).encode("ascii")
    return f"tool_error:{sha256(encoded).hexdigest()}"


def _fingerprint(name: str, arguments: str, category: str, version: str | None) -> str:
    """规范化 JSON 后只保留摘要，状态占用不随参数和结果体积增长。"""
    try:
        normalized = json.dumps(
            json.loads(arguments),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        argument_kind = "json"
    except (ValueError, RecursionError):
        normalized, argument_kind = arguments.strip(), "invalid_json"
    payload = json.dumps([name, argument_kind, normalized, category, version], ensure_ascii=True)
    return sha256(payload.encode("utf-8")).hexdigest()

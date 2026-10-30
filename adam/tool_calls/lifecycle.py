"""每次工具调用的 Hook 协调：先可靠留痕，再执行本地检查器。"""

import copy
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

from adam.contracts.errors import HookLogError, StorageError, ToolHookError
from adam.contracts.execution import ExecutionMode
from adam.contracts.hooks import HookEvent, HookRequest, HookResult, HookService
from adam.session.storage import SessionStore


class ToolHooks:
    """绑定一个工具来源；前置不通过不进入动作，后置只观察已落盘结果。"""

    def __init__(
        self,
        service: HookService,
        store: SessionStore,
        session_id: str,
        link: dict[str, str],
        notify: Callable[[str], None],
    ) -> None:
        self.service, self.store, self.session_id = service, store, session_id
        self.link, self.notify = dict(link), notify
        self._request: HookRequest | None = None

    def before(self, name: str, arguments: dict[str, Any], mode: ExecutionMode) -> None:
        """参数、模式和规则通过后执行；保存失败向上传播，拒绝转为独立工具状态。"""
        request = HookRequest(
            HookEvent.BEFORE_TOOL,
            name,
            copy.deepcopy(arguments),
            {"session_id": self.session_id, **self.link},
            mode,
        )
        result = self._run(request)
        if result is not None and result.status != "allowed":
            detail = result.message or result.error_code or "未获允许"
            raise ToolHookError(f"前置 Hook 阻止本次工具，未执行：{detail}")
        # 只为已通过前置边界的工具保留后置快照；处理函数修改参数也不能改变审计来源。
        self._request = request

    def after(self, output: str) -> None:
        """在 tool_finished 落盘后观察实际回传结果；检查失败仅提示，不改写工具输出。"""
        previous = self._request
        if previous is None:
            return
        result = self._run(
            HookRequest(
                HookEvent.AFTER_TOOL,
                previous.tool_name,
                previous.arguments,
                previous.source,
                previous.mode,
                json.loads(output),
            )
        )
        if result is not None and result.status == "failed":
            self.notify(
                f"后置 Hook 检查失败（{result.error_code}）；工具实际结果已保存，未回滚或重做。"
            )

    def _run(self, request: HookRequest) -> HookResult | None:
        """开始记录成功才启动检查；取消尽力结算，审计故障不能降级为普通工具失败。"""
        if not self.service.configured(request.event):
            return None
        link = {
            **self.link,
            "hook_id": secrets.token_hex(16),
            "event": request.event.value,
            "execution_mode": request.mode.value,
        }
        self._record("hook_started", link)
        started = time.monotonic()
        try:
            result = self.service.run(request)
        except KeyboardInterrupt as error:
            try:
                self._record(
                    "hook_finished",
                    {
                        **link,
                        "result": {
                            "status": "interrupted",
                            "message": "本地 Hook 被取消，未自动重试。",
                            "error_code": "cancelled",
                            "exit_code": None,
                            "duration_ms": round((time.monotonic() - started) * 1000),
                            "stderr": "",
                        },
                    },
                )
            except HookLogError as log_error:
                error.add_note(str(log_error))
            raise
        if result is None:
            raise HookLogError("已开始的 Hook 没有检查结果，已停止本轮。")
        self._record("hook_finished", {**link, "result": result.to_record()})
        return result

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        """公开存储失败或关联校验失败都停止本轮，保留上一次可靠落盘的事实。"""
        try:
            self.store.append_hook_event(self.session_id, kind, payload)
        except (StorageError, ValueError) as error:
            raise HookLogError("Hook 审计记录保存失败；已停止本轮，未自动重试。") from error

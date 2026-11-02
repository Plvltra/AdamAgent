"""原工具管线中的Android观察和逐次授权动作；默认不启用或传给子任务。"""

import json
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from typing import Any
from uuid import uuid4

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.media import MediaSource
from adam.contracts.mobile import MobileActionUnknownError, MobileApproval
from adam.contracts.models import ExecutionRecorder
from adam.mobile.actions import perform, plan_action
from adam.mobile.observation import AndroidObserver, MobileObservation
from adam.session.media_evidence import SessionMedia
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

SPECS = {
    "mobile_observe": schema(
        "观察宿主授权的固定Android测试应用；返回层级、版本和截图引用。", {}, []
    ),
    "mobile_action": schema(
        "逐次批准后click/input/swipe/back；用最新version，动作后重新观察，未知不能重试。",
        {
            "version": ("string", "最近观察版本"),
            "operation": ("string", "click/input/swipe/back"),
            "control": ("string", "click的唯一content-desc"),
            "value": ("string", "input的ASCII正文"),
            "x": ("integer", "swipe起点x"),
            "y": ("integer", "swipe起点y"),
            "x2": ("integer", "swipe终点x"),
            "y2": ("integer", "swipe终点y"),
        },
        ["version", "operation"],
    ),
}


class MobileTools:
    """原来源/模式/Hook/预算路径；动作版本一次使用，失败不恢复旧引用。"""

    def __init__(
        self,
        observer: AndroidObserver,
        media: SessionMedia,
        confirm: Callable[[MobileApproval], bool] | None,
        max_chars: int,
    ) -> None:
        self.observer, self.media, self.confirm, self.max_chars = (
            observer,
            media,
            confirm,
            max_chars,
        )
        self.source: dict[str, str] | None = None
        self.last: tuple[str, str, MobileObservation] | None = None

    def bind_source(self, source: dict[str, str]) -> None:
        """只接受原执行器注入的真实来源，模型参数不能指定会话或工具身份。"""
        self.source = dict(source)

    def close(self) -> None:
        """中断/退出仅丢弃动作引用；ADB客户端由各次调用finally回收。"""
        self.last = None

    def direct(
        self, name: str, args: dict[str, Any], record: ExecutionRecorder | None
    ) -> dict[str, Any]:
        """直接注册表调用仍须真实活动工具来源和逐次批准。"""
        return self.invoke(name, args, record, CancellationToken())

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        record: ExecutionRecorder | None,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """批准前后都检查页面，动作后观察失败标未知并终止轮次，不补发。"""
        try:
            with self.observer.lease():
                sid = self.media.store.active_session_id
                if not sid or self.source is None:
                    raise ToolError("Mobile requires active tool source")
                source = MediaSource(sid, kind="tool", tool_name=name, **self.source)
                self.media.store.validate_media_source(source)
                cancel.check()
                if name == "mobile_observe":
                    self.last = None
                    return self._observe(sid, source, cancel, "observed")
                previous, self.last = self.last, None
                if previous is None or previous[:2] != (sid, args["version"]):
                    raise ToolError("Stale mobile observation; observe again")
                frame = self.observer.require_current(previous[2].frame.version, cancel)
                plan = plan_action(args, frame)
                if self.confirm is None or not self.confirm(plan):
                    return {"ok": False, "status": "denied"}
                cancel.check()
                if self.media.store.active_session_id != sid:
                    raise ToolError("Session changed during mobile approval")
                self.observer.require_current(frame.version, cancel)
                perform(self.observer, plan, cancel)
                try:
                    return self._observe(sid, source, cancel, "dispatched")
                except BaseException as error:
                    raise MobileActionUnknownError(
                        "Mobile action dispatched but observation failed; do not retry"
                    ) from error
        except RuntimeError as error:
            self.last = None
            raise ToolError(str(error)) from error
        except BaseException:
            self.last = None
            raise

    def _observe(
        self, sid: str, source: MediaSource, cancel: CancellationToken, status: str
    ) -> dict[str, Any]:
        """完整结果超长则不交付动作引用；同页面每次观察也生成唯一消费身份。"""
        observed = self.observer.observe(source, self.media.publish, cancel)
        frame = asdict(observed.frame)
        frame.pop("png")
        version = observed.frame.version + ":" + uuid4().hex
        result = dict(
            ok=True, status=status, version=version, device=frame, image=asdict(observed.image)
        )
        if len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
            raise ToolError("Mobile observation exceeds output limit; no actionable reference")
        self.last = (sid, version, observed)
        return result


def register_mobile(
    registry: ToolRegistry,
    observer: AndroidObserver,
    media: SessionMedia,
    confirm: Callable[[MobileApproval], bool] | None,
) -> tuple[ToolRegistry, MobileTools]:
    """宿主显式注入模拟器才注册；主任务专用，不继承给子任务。"""
    tools = MobileTools(observer, media, confirm, registry.limits.max_result_chars)
    return registry.extended(
        SPECS,
        {name: partial(tools.direct, name) for name in SPECS},
        capabilities={
            "mobile_observe": ToolCapability.SESSION_STATE,
            "mobile_action": ToolCapability.EXTERNAL_ACTION,
        },
        interruptible_handlers={name: partial(tools.invoke, name) for name in SPECS},
        groups=dict.fromkeys(SPECS, "mobile"),
    ), tools

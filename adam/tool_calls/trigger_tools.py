"""主模型本地提醒工具；会话与轮次来自真实执行来源，模型不能指定目标运行。"""

from collections.abc import Callable
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry


class TriggerTools:
    """仅live-input主会话注册；调度回调由宿主注入，不在工具层操作事件队列。"""

    def __init__(
        self, dispatch: Callable[[str, str, dict[str, Any]], dict[str, Any]], store: SessionStore
    ) -> None:
        self.dispatch, self.store = dispatch, store
        self._source: dict[str, str] = {}

    def bind_source(self, source: dict[str, str]) -> None:
        """保存执行器绑定的真实轮次来源，不接受模型覆盖。"""
        self._source = dict(source)

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        """登记一次性提醒；只在当前轮次安全点检查，不唤醒已结束会话。"""
        spec = schema(
            "本地一次性提醒，仅当前轮次有效，安全点检查，不常驻等待。action为timer/watch/cancel/list。"
            "timer用delay_seconds及text；watch用job_id及condition(output/completed)，completed不是成功。"
            "cancel用timer_id。轮次结束后的提醒不自动启动新请求。",
            {
                "action": ("string", "timer/watch/cancel/list"),
                "delay_seconds": ("integer", "从现在起0～86400秒"),
                "text": ("string", "提醒正文"),
                "job_id": ("string", "当前会话后台任务ID"),
                "condition": ("string", "output或completed"),
                "timer_id": ("string", "要取消的提醒ID"),
            },
            ["action"],
        )
        return registry.extended(
            {"local_trigger": spec},
            {"local_trigger": self.handle},
            capabilities={"local_trigger": ToolCapability.SESSION_STATE},
            groups={"local_trigger": "commands"},
        )

    def handle(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """转发固定会话与轮次；参数错误返回工具错误，存储错误继续停止本轮。"""
        owner, turn_id = self.store.active_session_id, self._source.get("turn_id")
        if owner is None or not turn_id:
            raise ToolError("本地提醒缺少当前会话或轮次。")
        try:
            return self.dispatch(owner, turn_id, args)
        except (ValueError, KeyError, TypeError) as error:
            raise ToolError(f"本地提醒参数无效：{error}") from error

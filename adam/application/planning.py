"""主轮次计划服务；校验模型更新并保存事实，不直接执行文件或命令工具。"""

from typing import Any

from adam.config.settings import PlanningConfig
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.planning import PlanSnapshot, decode_steps, is_replan
from adam.session.storage import SessionStore


class PlanningService:
    """每轮重新绑定身份；计划内容和已完成证据以会话日志为准。"""

    def __init__(self, store: SessionStore, limits: PlanningConfig) -> None:
        self.store, self.limits = store, limits
        self._session_id = ""
        self._turn_id = ""
        self._source: dict[str, str] = {}

    def begin(self, session_id: str, turn_id: str) -> None:
        """绑定当前主轮次；不创建空计划，不加载其他轮次的进行中步骤。"""
        self._session_id, self._turn_id = session_id, turn_id
        self._source = {}

    def bind_source(self, link: dict[str, str]) -> None:
        """只接受执行器提供的来源，实际工具父记录在保存时再次验证。"""
        self._source = dict(link)

    def update(self, steps: object, reason: str = "") -> dict[str, Any]:
        """先验证模型参数和真实证据，再持锁追加；落盘失败停止本轮，不伪装为参数错误。"""
        if not self._session_id or self._source.get("turn_id") != self._turn_id:
            raise ToolError("计划缺少本轮有效调用来源。")
        report = self.store.load_plan(self._session_id, self._turn_id)
        previous = report.snapshot if report else None
        try:
            items = decode_steps(steps)
            changed = previous is not None and is_replan(previous, items)
            plan = PlanSnapshot(
                self._turn_id,
                self._source["request_id"],
                self._source["tool_call_id"],
                previous.version + 1 if previous else 1,
                items,
                reason,
                (previous.replan_count if previous else 0) + int(changed),
                previous.max_steps if previous else self.limits.max_steps,
                previous.max_replans if previous else self.limits.max_replans,
            )
            self.store.validate_plan(self._session_id, plan)
        except (ValueError, KeyError) as error:
            raise ToolError(f"计划未更新：{error}") from None
        try:
            self.store.append_plan(self._session_id, plan)
        except ValueError as error:
            # 预校验后的日志契约错误属于存储异常，不能被注册表转成可继续执行的错误。
            raise StorageError("计划日志校验失败，已停止本轮；未自动重做。") from error
        return {
            "version": plan.version,
            "replan_count": plan.replan_count,
            "steps": [step.to_dict() for step in plan.steps],
        }

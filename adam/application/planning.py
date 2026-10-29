"""主轮次计划服务；校验模型更新并保存事实，不直接执行文件或命令工具。"""

from collections.abc import Callable
from typing import Any

from adam.config.settings import PlanningConfig
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.planning import PlanReport, PlanSnapshot, decode_steps, is_replan
from adam.session.storage import SessionStore


class PlanningService:
    """每轮重新绑定身份；计划内容和已完成证据以会话日志为准。"""

    def __init__(
        self,
        store: SessionStore,
        limits: PlanningConfig,
        *,
        on_change: Callable[[PlanReport], None] | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.store, self.limits = store, limits
        self._session_id = ""
        self._turn_id = ""
        self._source: dict[str, str] = {}
        self._on_change = on_change
        self._notify = notify or (lambda _: None)
        self._pending: PlanSnapshot | None = None
        self._reminded = False

    def begin(self, session_id: str, turn_id: str) -> None:
        """绑定当前主轮次；不创建空计划，不加载其他轮次的进行中步骤。"""
        self._session_id, self._turn_id = session_id, turn_id
        self._source = {}
        self._pending = None
        self._reminded = False

    def unfinished(self) -> bool:
        """从日志查询本轮待办，不依赖上次更新时的缓存状态。"""
        report = self.store.load_plan(self._session_id, self._turn_id) if self._session_id else None
        return report is not None and bool(report.snapshot.unfinished)

    def retry_final(self, can_request: bool) -> bool:
        """最多追加一次收尾机会；额度耗尽时保留回复并明确未完成。"""
        if not self.unfinished():
            return False
        if not self._reminded and can_request:
            self._reminded = True
            self._notify("计划> 仍有未闭合步骤，提醒模型继续执行或说明失败/取消原因。")
            return True
        self._notify("计划> 计划未完成；保留已有结果，请用 /plan 查看剩余步骤，不自动重跑。")
        return False

    def instructions(self, session_id: str | None) -> str:
        """只提供程序固定提醒，用户/模型写的计划正文保留在工具消息中。"""
        if not self._reminded or session_id != self._session_id:
            return ""
        report = self.store.load_plan(self._session_id, self._turn_id)
        if report is None or report.turn_status != "in_progress":
            return ""
        return (
            "\n计划收尾提醒：上一次最终回答仍有未闭合步骤。请完成剩余工作，"
            "或通过 update_plan 明确 failed/cancelled 及原因，再给最终回答；"
            "不得用虚构或失败的工具证据标记 completed。"
        )

    def publish(self) -> None:
        """工具结果落盘后再展示；回调位于注册表之外，终端错误不得伪装成工具失败。"""
        pending, self._pending = self._pending, None
        if pending is not None and self._on_change:
            self._on_change(PlanReport(pending, "in_progress"))

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
        self._pending = plan
        return {
            "version": plan.version,
            "replan_count": plan.replan_count,
            "steps": [step.to_dict() for step in plan.steps],
        }

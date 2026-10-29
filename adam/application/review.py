"""协调独立审查：主 Agent 修改，子 Agent 只读检查；结论由真实子日志校验。"""

import json
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from adam.config.settings import ReviewConfig, ToolLimits
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.review import validate_request, validate_result
from adam.session.storage import SessionStore
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot

if TYPE_CHECKING:
    from adam.application.subagents.manager import SubagentManager


class ReviewService:
    """保存每次审查的范围、版本和来源，不把模型意见当成验证成功。"""

    def __init__(
        self,
        store: SessionStore,
        manager: "SubagentManager",
        policy: PathPolicy,
        limits: ToolLimits,
        config: ReviewConfig,
        notify: Callable[[str], None],
    ) -> None:
        self.store, self.manager, self.policy = store, manager, policy
        self.limits, self.config, self.notify = limits, config, notify
        self.session_id = ""
        self.turn_id = ""
        self.source: dict[str, str] = {}

    def begin(self, session_id: str, turn_id: str) -> None:
        """只绑定当前轮次；旧结论通过日志读取，不复用旧子任务。"""
        self.session_id, self.turn_id, self.source = session_id, turn_id, {}

    def bind_source(self, link: dict[str, str]) -> None:
        """来源由执行器注入，模型不能指定父请求。"""
        self.source = dict(link)

    def pending(self) -> bool:
        """未收集的审查阻止最终提交，也暂停有副作用的主工具。"""
        return any(item["status"] == "pending" for item in self._records())

    def check_call(self, name: str) -> None:
        """审查中保留只读与收集能力，防止检查过程中修改被审对象。"""
        if self.pending() and name in {
            "replace_text",
            "run_command",
            "spawn_agent",
            "send_message",
        }:
            raise ToolError("审查尚未收集；请先 collect_review，再修改或派发其他任务。")

    def request(self, goal: str, criteria: str, paths: list[str]) -> dict[str, Any]:
        """冻结文件版本并启动只读子任务；落盘失败向上传播并结束本轮。"""
        previous = self._records()
        if self.pending() or len(previous) >= self.config.max_rounds:
            raise ToolError("审查尚未收集或已达到本轮审查次数上限。")
        if not self.manager.accept_final():
            raise ToolError("请先收集已有子任务，再启动串行审查。")
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 8
            or any(not isinstance(path, str) for path in paths)
        ):
            raise ToolError("paths 必须包含 1～8 个文件路径。")
        targets = [self._snapshot(path) for path in paths]
        review_id = secrets.token_hex(4)
        record = {
            **self.source,
            "review_id": review_id,
            "agent_id": "pending",
            "run_id": "pending",
            "round": len(previous) + 1,
            "max_rounds": self.config.max_rounds,
            "goal": goal,
            "criteria": criteria,
            "targets": targets,
        }
        validate_request(record)
        task = json.dumps(
            {"goal": goal, "criteria": criteria, "targets": targets}, ensure_ascii=False
        ) + (
            "\n自行 read_file 后仅返回 JSON："
            '{"verdict":"findings|no_findings|incomplete","summary":"简述",'
            '"evidence":["本子轮读取调用ID"],"findings":['
            '{"path":"targets中的路径","line":1,"severity":"high|medium|low",'
            '"message":"可操作的问题","evidence_id":"读取调用ID"}]}。'
            "no_findings 必须完整读过每个目标；依据不足用 incomplete，findings 可为空。"
        )
        child = self.manager.spawn("审查 " + review_id, task, role="reviewer")
        record.update(agent_id=child["agent_id"], run_id=child["run_id"])
        self._append("review_requested", record)
        return {"review_id": review_id, "status": "pending", "round": record["round"]}

    def collect(self, review_id: str, timeout_seconds: int = 30) -> dict[str, Any]:
        """只接收审查 ID；原始回复和读取凭据来自关联子轮，不接受模型自报证据。"""
        current = next((r for r in self._records() if r["review_id"] == review_id), None)
        if current is None:
            raise ToolError("审查不属于当前轮次。")
        if current["status"] != "pending":
            return self._public(current)
        self.manager.wait([current["agent_id"]], timeout_seconds)
        try:
            details = self.manager.run_details(current["agent_id"], current["run_id"])
        except ToolError:
            return {"review_id": review_id, "status": "pending"}
        run = details["run"]
        reads = self._reads(details["tools"])
        state, reason, result = "failed", "reviewer_failed", None
        if run["status"] == "completed":
            state, reason = "invalid", "invalid_review_result"
            try:
                if details["role"] != "reviewer" or run["parent_turn_id"] != self.turn_id:
                    raise ValueError("wrong reviewer")
                result = validate_result(json.loads(run["reply"]), reads, current["targets"])
                state, reason = "completed", "reviewed"
            except (ValueError, TypeError, KeyError):
                result = None
            if not self._unchanged(current):
                state, reason, result = "stale", "target_changed", None
        elif run["status"] == "interrupted":
            state, reason = "interrupted", "reviewer_interrupted"
        outcome = {
            **self.source,
            "review_id": review_id,
            "status": state,
            "reason": reason,
            "child_turn_id": run["child_turn_id"],
            "reads": reads,
            "result": result,
        }
        self._append("review_finished", outcome)
        return self._public({**current, **outcome})

    def instructions(self) -> str:
        """提示协作规则及当前收尾要求；结论始终作为模型意见而非事实认证。"""
        if not self.session_id or not self._records():
            return ""
        return (
            "\n需要独立审查时用 request_review(goal,criteria,paths)，随后 collect_review。"
            "主 Agent 执行修改，审查者只读；修改后旧结论不再代表当前文件。"
            "收到 findings 后检查并修正，必要时复审；有限轮次内不能解决时明确报告。"
            "审查意见不是正确性的保证。当前审查："
            + json.dumps([self._public(r) for r in self._records()], ensure_ascii=False)
        )

    def finish(self) -> None:
        """最终展示残留问题和失效结论，不静默冒充审查通过。"""
        for record in self._records():
            result = record["result"]
            if not self._unchanged(record):
                self.notify("审查> 文件已变化，旧结论不代表当前版本。")
            elif record["status"] != "completed" or result["verdict"] != "no_findings":
                self.notify("审查> 仍有问题或审查未完成；请结合记录确认。")

    def _records(self) -> list[dict[str, Any]]:
        return self.store.load_reviews(self.session_id, self.turn_id) if self.session_id else []

    def _snapshot(self, path: str) -> dict[str, str]:
        """使用已有读取授权和大小限制；绝对规范路径使子证据能精确匹配。"""
        target = self.policy.resolve(path)
        return {
            "path": str(target),
            "version": read_snapshot(target, self.limits.max_text_file_bytes).version,
        }

    def _unchanged(self, record: dict[str, Any]) -> bool:
        try:
            return all(self._snapshot(t["path"]) == t for t in record["targets"])
        except (OSError, ToolError):
            return False

    def _reads(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """提取历史读取凭据；文件删除不抹掉旧证据，当前有效性由版本检查判断。

        截断或从中间开始的读取不能证明完整检查；已越界的路径不重新读取。
        """
        reads = []
        for tool in tools:
            if tool["name"] != "read_file" or not tool["finished"]:
                continue
            args, output = json.loads(tool["arguments"]), json.loads(tool["output"])
            if not output.get("ok") or "lines" not in output:
                continue
            try:
                target = self.policy.resolve(args["path"], must_exist=False)
            except (OSError, ToolError):
                # 外部移动或链接变化不能让收集一直停在 pending；子 JSONL 仍保留原凭据。
                continue
            reads.append(
                {
                    "tool_call_id": tool["tool_call_id"],
                    "path": str(target),
                    "version": output["version"],
                    "lines": [line["line"] for line in output["lines"]],
                    "complete": args.get("start_line", 1) == 1 and not output.get("truncated"),
                }
            )
        return reads

    def _append(self, kind: str, record: dict[str, Any]) -> None:
        """日志契约错误不能被注册表降级成可重试的工具参数错误。"""
        try:
            self.store.append_review(self.session_id, kind, record)
        except ValueError as error:
            raise StorageError("审查日志校验失败；已停止本轮。") from error

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: record[k] for k in ("review_id", "status", "result") if k in record}

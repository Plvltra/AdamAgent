"""组织会话流程；会话文件是已完成历史的来源。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.config import AgentConfig
from adam.llm import Completion, LLMClient, LLMError
from adam.storage import Session, SessionStore, StorageError
from adam.tool_calls import ToolRegistry
from adam.tool_calls.executor import ToolExecutor


class TurnSaveError(RuntimeError):
    """模型已回答，但本轮未能保存。"""

    def __init__(self, reply: str) -> None:
        super().__init__("本轮保存失败，未加入历史；未自动重新请求模型。")
        self.reply = reply


class Conversation:
    def __init__(
        self,
        store: SessionStore,
        client: LLMClient,
        system_prompt: str,
        tools: ToolRegistry | None = None,
        limits: AgentConfig | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.system_prompt = system_prompt
        self.session: Session | None = None
        self.model = client.config.model
        self.tool_registry = tools or ToolRegistry(Path.cwd())
        self.limits = limits or AgentConfig()
        self.notify = notify or (lambda _: None)
        self.tool_executor = ToolExecutor(store, self.tool_registry, self.notify)

    def new(self, title: str = "新对话") -> Session:
        """创建并选中新会话，返回会话元信息。"""
        session_id = self.store.create_session(title, self.system_prompt)
        return self.resume(session_id)

    def resume(self, session_id: str) -> Session:
        """验证历史并切换会话；失败时保留原会话。"""
        session = self.store.select_session(session_id)
        self.store.load_messages(session_id)
        self.session = session
        return session

    def send(self, text: str) -> str:
        """处理用户输入，完成模型与工具循环，返回已保存的最终回答。

        输入、请求和工具轨迹独立留痕；失败轮次不加入有效历史。预算或协议错误抛出
        LLMError；回答生成后保存失败则抛出携带回答的 TurnSaveError。
        工具诊断写入失败直接抛出 OSError 并停止；未知命令状态抛出 StorageError，
        命令日志写入或关联校验失败抛出 CommandLogError。
        """
        session = self.session or self.new(text[:40])
        turn_id = self.store.start_turn(session.session_id, text)
        if any(
            item["status"] == "unknown" for item in self.store.load_executions(session.session_id)
        ):
            raise StorageError("会话有结果未知的命令执行；请检查执行记录并新建会话，不自动重跑。")
        messages = self._load_context(session)
        turn: list[dict[str, Any]] = [{"role": "user", "content": text}]
        seen_call_ids: set[str] = set()

        for step in range(self.limits.max_model_requests):
            request_id, result = self._request_with_usage(
                session.session_id, turn_id, messages + turn
            )
            turn.append(result.message)
            calls = result.message.get("tool_calls") or []
            if not calls:
                return self._save_completed_turn(session.session_id, turn_id, turn)

            self._check_tool_budget(session.session_id, step, calls, seen_call_ids)
            turn.extend(self.tool_executor.execute(session.session_id, turn_id, request_id, calls))
            seen_call_ids.update(call["id"] for call in calls)

        raise LLMError("达到本轮请求上限。")

    def _load_context(self, session: Session) -> list[dict[str, Any]]:
        """组装系统提示与完整历史，保留工具调用和服务商推理字段。"""
        messages: list[dict[str, Any]] = (
            [{"role": "system", "content": session.system_prompt}] if session.system_prompt else []
        )
        messages.extend(message.raw for message in self.store.load_messages(session.session_id))
        return messages

    def _check_session_budget(self, session_id: str) -> None:
        """用量未知或累计额度耗尽时停止，避免静默漏记后继续请求。"""
        tokens, unknown = self.store.usage(session_id)
        if unknown:
            raise LLMError("会话预算统计不完整（用量未知或请求中断）；请新建会话，不自动重发。")
        if tokens >= self.limits.max_session_tokens:
            raise LLMError("会话 token 额度已用完，请提高配置额度或新建会话。")

    def _request_with_usage(
        self, session_id: str, turn_id: str, messages: list[dict[str, Any]]
    ) -> tuple[str, Completion]:
        """先持久登记请求，再调用 API 并结算用量；不自动重试。

        返回本次 request_id 与包含可靠用量的 Completion，供工具关联来源请求。
        turn_id 指向已记录的用户输入；请求中断留下的未结算记录
        会阻止恢复会话后继续消费。最终回答记账失败时保留回答供终端显示。
        """
        self._check_session_budget(session_id)
        request_id = self.store.start_request(session_id, turn_id, self.model)
        try:
            result = self.client.complete(
                messages,
                model=self.model,
                tools=self.tool_registry.definitions(),
                max_tokens=self.limits.max_output_tokens,
            )
        except LLMError as error:
            self.store.finish_request(session_id, turn_id, request_id, error.usage)
            raise

        try:
            self.store.finish_request(session_id, turn_id, request_id, result.usage)
        except OSError as error:
            if not result.message.get("tool_calls"):
                raise TurnSaveError(result.message["content"]) from error
            raise
        if result.usage is None:
            raise LLMError("API未返回可靠用量；预算统计不完整，本轮停止。")
        return request_id, result

    def _check_tool_budget(
        self, session_id: str, step: int, calls: list[dict[str, Any]], seen_ids: set[str]
    ) -> None:
        """执行整批工具前检查额度及重复 ID；step 为从零开始的模型请求序号。"""
        # 没有下一次模型请求额度时，不执行无法用于生成最终回答的工具。
        if step + 1 >= self.limits.max_model_requests:
            raise LLMError("达到本轮模型请求次数上限，工具链未加入有效历史。")
        if len(seen_ids) + len(calls) > self.limits.max_tool_calls:
            raise LLMError("达到本轮工具调用次数上限，工具链未加入有效历史。")
        self._check_session_budget(session_id)
        if any(call["id"] in seen_ids for call in calls):
            raise LLMError("模型重复使用工具调用ID，本轮停止。")

    def _save_completed_turn(
        self, session_id: str, turn_id: str, turn: list[dict[str, Any]]
    ) -> str:
        """提交包含工具轨迹的完整轮次并返回最终文本；保存失败时携带原回答。"""
        reply = turn[-1]["content"]
        try:
            self.store.append_messages(session_id, turn_id, turn)
        except OSError as error:
            raise TurnSaveError(reply) from error
        return reply

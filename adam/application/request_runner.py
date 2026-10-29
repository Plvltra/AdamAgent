"""共享物理请求执行；聊天与反思统一登记、结算、取消和失败收尾。"""

from collections.abc import Callable
from contextlib import nullcontext
from time import monotonic_ns
from typing import Any

from adam.context.builder import RequestContext
from adam.contracts.cancellation import CancellationToken
from adam.contracts.delegation import RequestAccount
from adam.contracts.errors import (
    FailureCode,
    LLMError,
    RequestInterrupted,
    RequestNotSentError,
    StorageError,
    TurnSaveError,
)
from adam.contracts.models import Completion, ModelOutput
from adam.contracts.ports import ModelClient
from adam.session.outcome import ExecutionOutcome
from adam.session.storage import SessionStore


class RequestRunner:
    """执行一次物理请求；重试次数和是否值得反思由外层协调器决定。"""

    def __init__(
        self,
        store: SessionStore,
        client: ModelClient,
        check_budget: Callable[[str], None],
        accounting: Callable[[str], RequestAccount] | None = None,
        cancel: CancellationToken | None = None,
        clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self.store, self.client, self.check_budget = store, client, check_budget
        self.accounting, self.cancel, self.clock = accounting, cancel, clock

    def run(
        self,
        session_id: str,
        turn_id: str,
        context: RequestContext,
        on_output: Callable[[ModelOutput], None] | None = None,
        *,
        record_context: dict[str, Any] | None = None,
    ) -> tuple[str, Completion]:
        """执行一次物理请求：检查预算、登记、调用和结算；外层策略决定是否重试。

        record_context 仅用于专门反思的来源记录；普通请求沿用上下文选择记录。
        反思协议错误且用量已结算时返回空正文，保留请求失败记录，调用方再记录无效反思。
        返回本次 request_id 与包含可靠用量的 Completion，供工具关联来源请求。
        turn_id 指向已记录的用户输入；context 是本次构造结果，先检查输入预算再登记请求。
        本地超限不伪造请求或未知用量；有效响应先记请求成功，缺少用量时整轮仍停止。
        明确未发出的连接错误结算为零；只有结算成功才允许外层重试。
        中断或未知用量会阻止继续消费。最终回答记账失败时保留回答供终端显示；
        请求耗时只包含完整模型调用及响应解析，不含开始与结算日志写入。
        失败收尾不遮蔽原始异常；崩溃遗留状态由下次取得会话写锁时恢复。
        """
        self.check_budget(session_id)
        context.ensure_within_budget()
        scope = (
            self.accounting(session_id).request(
                context.budgeted_input_tokens + context.max_output_tokens
            )
            if self.accounting
            else nullcontext()
        )
        with scope:
            return self._perform_request(session_id, turn_id, context, on_output, record_context)

    def _perform_request(
        self,
        session_id: str,
        turn_id: str,
        context: RequestContext,
        on_output: Callable[[ModelOutput], None] | None,
        record_context: dict[str, Any] | None,
    ) -> tuple[str, Completion]:
        """在额度预留内登记、调用与结算；共享额度退出时读取本次持久化结果。"""
        if self.cancel:
            self.cancel.check()
        request_id = self.store.start_request(
            session_id, turn_id, context.model, context=record_context or context.selection_record()
        )
        is_reflection = (
            record_context is not None and record_context.get("strategy") == "reflection_v1"
        )
        started_ns = self.clock()
        try:
            result = self.client.complete(
                context.messages,
                model=context.model,
                tools=context.tools,
                max_tokens=context.max_output_tokens,
                on_output=on_output,
            )
        except BaseException as error:
            usage = error.usage if isinstance(error, (LLMError, RequestInterrupted)) else None
            outcome = ExecutionOutcome.failure(error, (self.clock() - started_ns) // 1_000_000)
            degradable = (
                is_reflection
                and isinstance(error, LLMError)
                and error.code == FailureCode.INVALID_RESPONSE
                and error.usage is not None
            )
            if isinstance(error, RequestNotSentError) or degradable:
                # 未发出请求允许重试、反思格式错误允许降级，都必须先可靠保存失败及用量。
                self.store.finish_request(session_id, turn_id, request_id, usage, outcome=outcome)
            else:
                save_failure(
                    lambda: self.store.finish_request(
                        session_id, turn_id, request_id, usage, outcome=outcome
                    ),
                    error,
                )
            if degradable:
                return request_id, Completion({"role": "assistant", "content": ""}, usage)
            raise

        outcome = ExecutionOutcome.success((self.clock() - started_ns) // 1_000_000)
        try:
            self.store.finish_request(
                session_id, turn_id, request_id, result.usage, outcome=outcome
            )
        except StorageError as error:
            if not is_reflection and not result.message.get("tool_calls"):
                raise TurnSaveError(
                    result.message["content"], code=FailureCode.REQUEST_SAVE_FAILED
                ) from error
            raise StorageError(
                "请求结算记录保存失败；已停止本轮，未自动重试。",
                code=FailureCode.REQUEST_SAVE_FAILED,
            ) from error
        if result.usage is None:
            raise LLMError(
                "API未返回可靠用量；预算统计不完整，本轮停止。", code=FailureCode.USAGE_UNKNOWN
            )
        return request_id, result


def save_failure(save: Callable[[], None], original: BaseException) -> None:
    """尽力保存失败状态；日志故障不能覆盖最初的模型、工具或程序异常。"""
    try:
        save()
    except Exception:
        # 磁盘持续不可写时无法再可靠记录终态；附注不带原始系统或服务端详情。
        original.add_note("执行状态保存失败，日志可能仍显示进行中；未自动重试。")

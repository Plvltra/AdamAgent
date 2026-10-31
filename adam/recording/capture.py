"""显式单轮录制：透明采集模型、工具和实际会话增量，不绕过原执行流程。"""

from collections.abc import Callable
from typing import Any, TypeVar

from adam.contracts.execution import ToolBudget
from adam.contracts.models import Completion, ModelOutput
from adam.contracts.ports import ModelClient, ToolRunner
from adam.recording.format import RecordingError, TraceState, error_record
from adam.recording.journal import RecordingWriter

_Result = TypeVar("_Result")


class TurnRecorder:
    """围绕调用边界留痕；调用方只为选定一轮创建实例，结束后不可继续录制。"""

    def __init__(
        self,
        writer: RecordingWriter,
        events: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.writer, self._events = writer, events
        # JSON 值由 writer 编码冻结；游标仅记录已采集条数，不修改业务日志。
        self._offset = writer.seed_event_count
        self._started = False
        self._running = False

    def run(self, operation: Callable[[], str]) -> str:
        """执行选定的一轮并记录终态；失败仍抛原异常，录制故障不触发动作重试。"""
        if self._started:
            raise RecordingError("一个录制器只能执行一个用户轮次。")
        self._started = True
        self._running = True
        try:
            try:
                reply = operation()
            except BaseException as error:
                failure = error_record(error)
                self._preserve_failure(lambda: self._finish(None, failure), error)
                raise
            self._finish(reply, None)
            return reply
        finally:
            self._running = False

    @property
    def active(self) -> bool:
        """仅选定轮次执行期间为真；组装、目录预览与关闭资源不属于该轮录制。"""
        return self._running

    def exchange(
        self,
        channel: str,
        operation: str,
        arguments: dict[str, Any],
        action: Callable[[], _Result],
    ) -> _Result:
        """采集宿主资料接口的实际结果；原动作失败仍保留原异常，不把数据当执行代码。"""
        self._require_active()
        try:
            value = action()
        except BaseException as error:
            failure = error_record(error)
            self._preserve_failure(
                lambda: self.observe(
                    channel,
                    {
                        "operation": operation,
                        "input": arguments,
                        "value": None,
                        "error": failure,
                    },
                ),
                error,
            )
            raise
        self.observe(
            channel, {"operation": operation, "input": arguments, "value": value, "error": None}
        )
        return value

    def observe(self, channel: str, value: dict[str, Any]) -> None:
        """记录本轮运行资料的快照；通道由宿主指定，内容不成为可执行配置。"""
        self._require_active()
        self.writer.append("observation", {"channel": channel, "value": value})

    def call_tools(
        self,
        operation: Callable[[], list[dict[str, Any]]],
        *,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
        edit_attempts: int,
        max_edit_attempts: int,
        provided_names: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """采集原执行器的整批调用、结果与内部记录；operation 仍执行全部原有检查。

        批次失败时保留已记录的前缀及受控错误，不返回虚构工具结果，不重跑工具。
        """
        return self._call(
            "tools",
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "request_id": request_id,
                "calls": calls,
                "edit_attempts": edit_attempts,
                "max_edit_attempts": max_edit_attempts,
                **({"provided_names": list(provided_names)} if provided_names is not None else {}),
            },
            lambda _: operation(),
            lambda value: value,
        )

    def call_model(
        self,
        client: ModelClient,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """采集传输接口的业务输入、输出及流式片段；不访问 client 的配置或 HTTP 对象。"""

        def invoke(call_id: int) -> Completion:
            def output(event: ModelOutput) -> None:
                self.writer.append("model_output", TraceState.output(call_id, event))
                if on_output:
                    on_output(event)

            return client.complete(
                messages, model=model, tools=tools, max_tokens=max_tokens, on_output=output
            )

        return self._call(
            "model",
            {
                "messages": messages,
                "model": model,
                "tools": tools,
                "max_tokens": max_tokens,
                "on_output": on_output is not None,
            },
            invoke,
            lambda result: {
                "message": result.message,
                "usage": list(result.usage) if result.usage is not None else None,
            },
        )

    def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        operation: Callable[[int], _Result],
        encode: Callable[[_Result], object],
    ) -> _Result:
        """输入可靠落盘后才调用原动作；结果与来源增量保存失败时禁止静默继续。"""
        self._require_active()
        call_id = self.writer.next_sequence
        self.writer.append("call_started", {"call_id": call_id, "name": name, "input": arguments})
        try:
            result = operation(call_id)
        except BaseException as error:
            failure = error_record(error)
            self._preserve_failure(
                lambda: self.writer.append(
                    "call_finished",
                    {
                        "call_id": call_id,
                        "value": None,
                        "error": failure,
                        "session_events": self._delta(),
                    },
                ),
                error,
            )
            raise
        self.writer.append(
            "call_finished",
            {
                "call_id": call_id,
                "value": encode(result),
                "error": None,
                "session_events": self._delta(),
            },
        )
        return result

    def _finish(self, reply: str | None, error: dict[str, Any] | None) -> None:
        """保存所选调用的终态与最后一段会话记录，不把录制完整等同于业务成功。"""
        self.writer.append(
            "recording_finished",
            {
                "reply": reply,
                "error": error,
                "session_events": self._delta(),
            },
        )

    def _delta(self) -> list[dict[str, Any]]:
        """从调用方持锁的会话取得未采集事件；读取失败沿原异常停止，不补造记录。"""
        events = self._events()
        if len(events) < self._offset:
            raise RecordingError("录制来源的会话日志变短，已停止采集。")
        delta = events[self._offset :]
        self._offset = len(events)
        return delta

    def _require_active(self) -> None:
        if not self._running:
            raise RecordingError("调用必须位于显式选定的录制轮次中。")

    @staticmethod
    def _preserve_failure(save: Callable[[], None], original: BaseException) -> None:
        """采集故障不能覆盖已有模型/工具异常；缺少结尾的录制会被读为不完整。"""
        try:
            save()
        except Exception:
            original.add_note("录制结束信息未能保存；录制可能不完整，未自动重试。")


class RecordingTools:
    """保留原执行器的参数校验、权限、并发和日志，仅在批次两侧采集记录。"""

    def __init__(
        self, delegate: ToolRunner, recorder: TurnRecorder, *, observe_definitions: bool = False
    ) -> None:
        self._delegate, self._recorder = delegate, recorder
        self._observe_definitions = observe_definitions

    def definitions(self, *, turn_id: str | None = None) -> list[dict[str, Any]]:
        """录制该轮实际说明或初始预览；选择仍由原执行器维护。"""
        if self._observe_definitions and self._recorder.active:
            return self._recorder.exchange(
                "context",
                "tools",
                {"turn_id": turn_id},
                lambda: self._delegate.definitions(turn_id=turn_id),
            )
        return self._delegate.definitions(turn_id=turn_id)

    def execute(
        self,
        session_id: str,
        turn_id: str,
        request_id: str,
        calls: list[dict[str, Any]],
        budget: ToolBudget,
        *,
        provided_names: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """捕获执行前的额度值，动作及额度消费仍交给原执行器。"""
        return self._recorder.call_tools(
            lambda: self._delegate.execute(
                session_id,
                turn_id,
                request_id,
                calls,
                budget,
                provided_names=provided_names,
            ),
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            calls=calls,
            edit_attempts=budget.edit_attempts,
            max_edit_attempts=budget.max_edit_attempts,
            provided_names=provided_names,
        )


class RecordingModel:
    """实现 ModelClient 的透明装饰器；目录查询不属于所选轮次的录制。"""

    def __init__(self, client: ModelClient, recorder: TurnRecorder) -> None:
        self._client, self._recorder = client, recorder

    def list_models(self) -> dict[str, str | None]:
        """透传目录查询；它不属于单轮执行轨迹。"""
        return self._client.list_models()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """保持调用者的响应与异常；额外记录不会主动发起第二次请求。"""
        return self._recorder.call_model(
            self._client,
            messages,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            on_output=on_output,
        )

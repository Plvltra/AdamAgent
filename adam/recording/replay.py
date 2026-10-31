"""有序消费录制的外部结果；比较真实调用输入，首次差异后停止，不访问原模型或工具。"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar, cast

from adam.contracts.errors import (
    AgentError,
    FailureCode,
    LLMError,
    RequestInterrupted,
    RequestNotSentError,
)
from adam.recording.format import RecordingError, error_record, validate_exchange
from adam.recording.journal import Recording

_Result = TypeVar("_Result")


@dataclass(frozen=True)
class ReplayDifference:
    """首次不同的录制序号、字段路径与值；输出层可缩短显示，不改变比较结果。"""

    sequence: int
    path: str
    expected: object
    actual: object


class ReplayMismatchError(RecordingError):
    """当前流程偏离录制；difference 指向首次观察到的差异。"""

    def __init__(self, difference: ReplayDifference) -> None:
        self.difference = difference
        super().__init__(f"重放在记录 {difference.sequence} 的 {difference.path} 出现差异。")


class ReplayIncompleteError(RecordingError):
    """已知前缀耗尽，不能推断缺失的外部结果或宣称整轮一致。"""


def first_difference(
    expected: object, actual: object, path: str = "$"
) -> tuple[str, object, object] | None:
    """按字段或列表位置找首个差异；保留类型差异，不把 True 当作 1。"""
    if type(expected) is not type(actual):
        return path, expected, actual
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                return f"{path}.{key}", expected[key], "<missing>"
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        for key in actual:
            if key not in expected:
                return f"{path}.{key}", "<absent>", actual[key]
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        if len(expected) != len(actual):
            return f"{path}.length", len(expected), len(actual)
        return None
    return None if expected == actual else (path, expected, actual)


def restore_error(value: dict[str, Any]) -> BaseException:
    """仅恢复白名单错误类别、原因及用量；不反序列化异常类、堆栈或任意代码。"""
    kind = value["kind"]
    code = FailureCode(value["code"])
    usage = tuple(value["usage"]) if value["usage"] is not None else None
    pair = (usage[0], usage[1]) if usage is not None else None
    if kind == "not_sent":
        return RequestNotSentError()
    if kind == "request_interrupted":
        return RequestInterrupted(pair)
    if kind == "interrupted":
        return KeyboardInterrupt()
    if kind == "exit":
        return SystemExit()
    if kind == "llm":
        return LLMError("录制中的模型失败。", pair, code=code, http_status=value["http_status"])
    if kind == "agent":
        return AgentError("录制中的业务失败。", code=code, http_status=value["http_status"])
    return RuntimeError("录制中的内部失败。")


class ReplayTape:
    """录制游标及首次停止原因；模型、工具与资料共用，不跳过缺失继续猜测。"""

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.position = 1
        self.difference: ReplayDifference | None = None
        self._incomplete: ReplayIncompleteError | None = None
        self.session_events = deepcopy(recording.seed["session_events"])
        self.session_offsets: dict[int, int] = {}
        for event in recording.events[1:]:
            if "session_events" in event["data"]:
                self.session_events.extend(deepcopy(event["data"]["session_events"]))
                self.session_offsets[event["seq"]] = len(self.session_events)

    def compare(
        self, expected: object, actual: object, path: str, *, sequence: int | None = None
    ) -> None:
        """发现差异时固定首个位置；后续清理中的错误不能覆盖原始诊断。"""
        self._check_stopped()
        difference = first_difference(expected, actual, path)
        if difference is not None:
            self.fail(*difference, sequence=sequence)

    def fail(
        self, path: str, expected: object, actual: object, *, sequence: int | None = None
    ) -> None:
        """记录并抛出首次差异，供不同适配器共享同一停止原因。"""
        self._check_stopped()
        if self.difference is None:
            self.difference = ReplayDifference(
                self.position if sequence is None else sequence, path, expected, actual
            )
        raise ReplayMismatchError(self.difference)

    def stop_incomplete(self, message: str) -> NoReturn:
        """固定缺失边界；后续清理不能将缺失升级成不同轨迹，也不能覆盖更早差异。"""
        self._check_stopped()
        self._incomplete = ReplayIncompleteError(message)
        raise self._incomplete

    def peek(self) -> dict[str, Any]:
        """查看下一条；数据缺失直接停止，不用空结果或成功占位。"""
        self._check_stopped()
        if self.position >= len(self.recording.events):
            self.stop_incomplete(f"录制在第 {self.position} 条之前结束，无法继续重放。")
        return self.recording.events[self.position]

    def take(self, kind: str) -> dict[str, Any]:
        """消费指定类型的下一条，严格保留模型、工具和资料查询的全局顺序。"""
        event = self.peek()
        self.compare(event["type"], kind, "$.type", sequence=event["seq"])
        self.position += 1
        return event

    def exchange(
        self,
        channel: str,
        operation: str,
        arguments: dict[str, Any],
        action: Callable[[], _Result],
    ) -> _Result:
        """返回录制的资料结果；action 仅为装饰接口兼容，重放永不调用它。"""
        event = self.take("observation")
        data = event["data"]
        self.compare(data["channel"], channel, "$.channel", sequence=event["seq"])
        value = data["value"]
        try:
            validate_exchange(value)
        except (ValueError, TypeError, KeyError) as error:
            raise RecordingError("资料记录不包含有效的调用契约。") from error
        self.compare(value["operation"], operation, "$.operation", sequence=event["seq"])
        self.compare(value["input"], arguments, "$.input", sequence=event["seq"])
        if value["error"] is not None:
            raise restore_error(value["error"])
        result = value["value"]
        if (channel, operation) in {("delegation", "accept_final"), ("review", "pending")} and type(
            result
        ) is not bool:
            raise RecordingError("状态观察必须返回布尔值。")
        if (
            channel in {"delegation", "review"}
            and operation == "instructions"
            and not isinstance(result, str)
        ):
            raise RecordingError("状态提示必须返回文本。")
        if (
            channel in {"rules", "delegation", "review"}
            and operation in {"begin", "end", "finish", "acknowledge", "set_memory", "check"}
            and result is not None
        ):
            raise RecordingError("无返回值的资料调用包含额外结果。")
        if (channel, operation) == ("context", "tools") and (
            not isinstance(result, list) or any(not isinstance(item, dict) for item in result)
        ):
            raise RecordingError("工具目录观察必须返回 Schema 列表。")
        # 各资料适配器还会校验具体内容；这里恢复同一接口的泛型结果类型。
        return cast(_Result, deepcopy(result))

    def identity(self, kind: str, size: int) -> str:
        """按原调用时序提供执行身份，随机生成差异不会污染流程比较。"""
        return self.exchange("context", "identity", {"kind": kind, "size": size}, lambda: "")

    def begin_call(self, name: str, arguments: dict[str, Any]) -> int:
        """在取得外部结果前核对调用种类及完整输入。"""
        event = self.take("call_started")
        self.compare(event["data"]["name"], name, "$.name", sequence=event["seq"])
        self.compare(event["data"]["input"], arguments, "$.input", sequence=event["seq"])
        return event["data"]["call_id"]

    def finish(self, reply: str | None, error: BaseException | None) -> None:
        """核对实际终态并要求录制完整消费；有结尾不表示业务本身成功。"""
        self._check_stopped()
        event = self.take("recording_finished")
        self.compare(event["data"]["reply"], reply, "$.reply", sequence=event["seq"])
        self.compare(
            event["data"]["error"],
            error_record(error) if error is not None else None,
            "$.error",
            sequence=event["seq"],
        )
        if not self.recording.complete or self.position != len(self.recording.events):
            self.stop_incomplete("录制缺少完整结尾，不能声明整轮重放一致。")

    def _check_stopped(self) -> None:
        """资料和日志共用首次停止原因；异常收尾不再推进录制游标。"""
        if self.difference is not None:
            raise ReplayMismatchError(self.difference)
        if self._incomplete is not None:
            raise self._incomplete

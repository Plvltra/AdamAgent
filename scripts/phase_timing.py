"""B34评估运行的阶段观测；实例级包装，不改变生产默认行为。"""

from collections.abc import Callable
from functools import wraps
from time import monotonic_ns
from typing import Any, ParamSpec, TypeVar

from adam.contracts.models import Completion, ModelOutput
from adam.contracts.ports import ModelClient

P = ParamSpec("P")
R = TypeVar("R")


class PhaseTiming:
    """保存本次试验的有界阶段记录；区间可能嵌套，禁止相加冒充墙钟耗时。"""

    def __init__(self) -> None:
        self.started = monotonic_ns()
        self.records: list[dict[str, Any]] = []
        self.dropped = 0

    def wrap(self, phase: str, operation: Callable[P, R]) -> Callable[P, R]:
        """包装一个实际调用，失败仍记录后原样抛出；不保存参数或异常正文。"""

        @wraps(operation)
        def timed(*args: P.args, **kwargs: P.kwargs) -> R:
            start = monotonic_ns()
            status = "failed"
            try:
                result = operation(*args, **kwargs)
                status = "completed"
                return result
            finally:
                self._record(phase, start, monotonic_ns(), status)

        return timed

    def bind_model(self, client: ModelClient) -> None:
        """记录完整模型调用和首段非空正文等待；非流式没有回调时保持未知。"""
        original = client.complete

        def complete(
            messages: list[dict[str, Any]],
            *,
            model: str,
            tools: list[dict[str, Any]] | None = None,
            max_tokens: int = 4096,
            on_output: Callable[[ModelOutput], None] | None = None,
        ) -> Completion:
            start = monotonic_ns()
            first: int | None = None
            callback = on_output

            def output(event: ModelOutput) -> None:
                nonlocal first
                if first is None and event.kind == "text" and event.text.strip():
                    first = monotonic_ns()
                if callback:
                    callback(event)

            try:
                return self.wrap("model", original)(
                    messages, model=model, tools=tools, max_tokens=max_tokens, on_output=output
                )
            finally:
                self._record("first_output", start, first, "observed" if first else "unknown")

        client.complete = complete

    def bind_store(self, store: object) -> None:
        """观测会话持久化入口（含序列化/校验），不声称覆盖所有磁盘或fsync耗时。"""
        for name in dir(store):
            if name.startswith(("append_", "start_", "finish_")) or name == "create_session":
                method = getattr(store, name)
                if callable(method):
                    setattr(store, name, self.wrap("storage", method))

    def snapshot(self) -> dict[str, Any]:
        """返回相对单调时钟区间；未采集阶段列为未知，截断数单独保留。"""
        return {
            "records": list(self.records),
            "dropped": self.dropped,
            "unobserved": [
                p
                for p in ("model", "first_output", "tool", "approval", "storage")
                if not any(r["phase"] == p and r["duration_ms"] is not None for r in self.records)
            ],
            "scope": "main_trial; inclusive_overlapping_intervals",
        }

    def _record(self, phase: str, start: int, end: int | None, status: str) -> None:
        if len(self.records) >= 10000:
            self.dropped += 1
            return
        self.records.append(
            {
                "phase": phase,
                "start_ms": (start - self.started) / 1e6,
                "end_ms": (end - self.started) / 1e6 if end is not None else None,
                "duration_ms": (end - start) / 1e6 if end is not None else None,
                "status": status,
            }
        )

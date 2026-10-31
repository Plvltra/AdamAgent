"""离线模型与工具适配器；只消费已验证记录，不持有网络客户端、工具注册表或子任务调度器。"""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from adam.contracts.execution import ToolBudget
from adam.contracts.models import Completion, ModelOutput
from adam.recording.replay import ReplayTape, restore_error
from adam.recording.replay_store import ReplayStore


class ReplayModel:
    """让原请求策略和结算逻辑得到录制响应，同时验证请求消息和参数。"""

    def __init__(self, tape: ReplayTape, store: ReplayStore) -> None:
        self.tape, self.store = tape, store

    def list_models(self) -> dict[str, str | None]:
        """目录来自录制模型名，不访问服务商。"""
        return {self.tape.recording.seed["model"]: None}

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """按原顺序交付流式片段及完整响应；错误仍经过生产重试与记账流程。"""
        call_id = self.tape.begin_call(
            "model",
            {
                "messages": messages,
                "model": model,
                "tools": tools,
                "max_tokens": max_tokens,
                "on_output": on_output is not None,
            },
        )
        while self.tape.peek()["type"] == "model_output":
            event = self.tape.take("model_output")
            data = event["data"]
            self.tape.compare(call_id, data["call_id"], "$.call_id", sequence=event["seq"])
            if on_output:
                on_output(ModelOutput(data["kind"], data["text"]))
        event = self.tape.take("call_finished")
        data = event["data"]
        self.tape.compare(call_id, data["call_id"], "$.call_id", sequence=event["seq"])
        self.store.at_boundary(event["seq"])
        if data["error"] is not None:
            raise restore_error(data["error"])
        value = data["value"]
        usage = value["usage"]
        return Completion(
            deepcopy(value["message"]), (usage[0], usage[1]) if usage is not None else None
        )


class ReplayTools:
    """提供原工具目录和结果；模型要求执行命令、改文件或启动子任务时也不产生外部动作。"""

    def __init__(self, tape: ReplayTape, store: ReplayStore) -> None:
        self.tape, self.store = tape, store

    def definitions(self, *, turn_id: str | None = None) -> list[dict[str, Any]]:
        """重放指定轮次实际说明，仍由真实构造器计算 Schema 开销；不执行发现工具。"""
        return self.tape.exchange(
            "context", "tools", {"turn_id": turn_id}, lambda: list[dict[str, Any]]()
        )

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
        """验证本批来源及编辑额度，导入工具内部事实后返回配对结果；不调用执行器。"""
        call_id = self.tape.begin_call(
            "tools",
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "request_id": request_id,
                "calls": calls,
                "edit_attempts": budget.edit_attempts,
                "max_edit_attempts": budget.max_edit_attempts,
                **({"provided_names": list(provided_names)} if provided_names is not None else {}),
            },
        )
        # 例如 report_feedback 在工具内部生成身份；这些资料属于已替代的工具实现。
        while self.tape.peek()["type"] == "observation":
            self.tape.take("observation")
        event = self.tape.take("call_finished")
        data = event["data"]
        self.tape.compare(call_id, data["call_id"], "$.call_id", sequence=event["seq"])
        self.store.apply_tool_events(event["seq"], budget)
        if data["error"] is not None:
            raise restore_error(data["error"])
        return deepcopy(data["value"])

"""按完整轮次选择模型输入并估算预算；不改写原始历史、不请求模型。"""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from adam.config.settings import ContextConfig
from adam.context.pruning import prune_history
from adam.contracts.errors import BudgetError, FailureCode
from adam.contracts.history import HistoryTurn, Summary


@dataclass(frozen=True)
class ContextEstimate:
    """输入各部分的近似 token 数；与服务端报告的计费用量分别使用。"""

    system_tokens: int
    history_tokens: int
    turn_tokens: int
    tools_tokens: int
    summary_tokens: int = 0

    @property
    def input_tokens(self) -> int:
        return (
            self.system_tokens
            + self.history_tokens
            + self.turn_tokens
            + self.tools_tokens
            + self.summary_tokens
        )


@dataclass(frozen=True)
class RequestContext:
    """一次构造的消息、工具及预算；消息与源历史分离，展示和发送使用同一份内容。"""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_output_tokens: int
    estimate: ContextEstimate
    limits: ContextConfig
    selected_turn_ids: tuple[str, ...]
    omitted_turn_ids: tuple[str, ...]
    original_history_tokens: int
    summary: Summary | None = None
    pruned_tools: tuple[dict[str, Any], ...] = ()
    candidate_history_tokens: int = 0

    @property
    def candidate_input_tokens(self) -> int:
        """工具缩减后、整轮选择前的输入估算，用于判断是否仍需摘要。"""
        return (
            self.estimate.input_tokens
            - self.estimate.history_tokens
            + self.candidate_history_tokens
        )

    def selection_record(self) -> dict[str, Any]:
        """返回实际请求的选择依据；不含认证信息，不是完整 HTTP 请求快照。"""
        return {
            "strategy": "recent_turns_v1",
            "summary_id": self.summary.summary_id if self.summary else None,
            "pruned_tools": list(self.pruned_tools),
            "selected_turn_ids": list(self.selected_turn_ids),
            "omitted_turn_ids": list(self.omitted_turn_ids),
            "estimated_input_tokens": self.estimate.input_tokens,
            "original_history_tokens": self.original_history_tokens,
        }

    @property
    def budgeted_input_tokens(self) -> int:
        """输入上限内包含安全余量；输出预留在输入上限之外，不重复扣除。"""
        return self.estimate.input_tokens + self.limits.safety_margin_tokens

    def ensure_within_budget(self) -> None:
        """超预算抛出公共 BudgetError；不发送请求，不裁剪或改写内容。"""
        excess = self.budgeted_input_tokens - self.limits.max_input_tokens
        if excess > 0:
            raise BudgetError(
                f"单次输入估算 {self.estimate.input_tokens} + 安全余量 "
                f"{self.limits.safety_margin_tokens} 超过输入上限 {self.limits.max_input_tokens}"
                f"（超出 {excess} token）；受保护内容无法放下，未发送本次请求。"
                "请缩短输入、新建会话或调整 [context] 配置。",
                code=FailureCode.CONTEXT_INPUT_LIMIT,
            )


class ContextBuilder:
    """保留系统约束和当前轮次，按预算选择最近连续成功轮次；预览与发送共用。"""

    def __init__(self, limits: ContextConfig, max_output_tokens: int) -> None:
        self.limits = limits
        self.max_output_tokens = max_output_tokens

    def build(
        self,
        *,
        model: str,
        system_prompt: str,
        history: list[HistoryTurn],
        current_turn: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        summary: Summary | None = None,
    ) -> RequestContext:
        """返回独立的输入副本及组成估算；保留顺序、工具配对和服务商推理字段。

        history 仅接收完整成功轮次；current_turn 包含最新输入及工具链，预览时为空。
        先保留摘要与配置指定的近期轮次；其余从新往旧取连续后缀，放不下即停止。
        系统提示、本轮及近期保护轮次不截断；摘要仅作为历史资料，不提升指令权限。
        预算覆盖完整工具 JSON Schema，不包含认证头、URL 或密钥。
        """
        covered = len(summary.covered_turn_ids) if summary else 0
        history = history[covered:]
        summary_messages = [summary.message()] if summary else []
        summary_tokens = sum(estimate_json_tokens(message) for message in summary_messages)
        system = [{"role": "system", "content": system_prompt}] if system_prompt else []
        system_tokens = sum(estimate_json_tokens(message) for message in system)
        turn_tokens = sum(estimate_json_tokens(message) for message in current_turn)
        tools_tokens = estimate_json_tokens(tools) if tools else 0
        sizes = [
            sum(estimate_json_tokens(message) for message in turn.messages) for turn in history
        ]
        original_history_tokens = sum(sizes)
        pressure = (
            system_tokens
            + turn_tokens
            + tools_tokens
            + summary_tokens
            + sum(sizes)
            + self.limits.safety_margin_tokens
        )
        pruned: tuple[dict[str, Any], ...] = ()
        if (
            self.limits.prune_tool_results
            and pressure >= self.limits.max_input_tokens * self.limits.compression_trigger_ratio
        ):
            history, pruned = prune_history(history, self.limits)
            sizes = [
                sum(estimate_json_tokens(message) for message in turn.messages) for turn in history
            ]
        remaining = (
            self.limits.max_input_tokens
            - self.limits.safety_margin_tokens
            - system_tokens
            - turn_tokens
            - tools_tokens
            - summary_tokens
        )
        start = len(history)
        protected_start = max(0, len(history) - self.limits.retain_recent_turns)
        for index in range(len(sizes) - 1, -1, -1):
            size = sizes[index]
            if size > remaining and index < protected_start:
                break
            remaining -= size
            start -= 1
        selected = history[start:]
        messages = [message for turn in selected for message in turn.messages]
        estimate = ContextEstimate(
            system_tokens, sum(sizes[start:]), turn_tokens, tools_tokens, summary_tokens
        )
        return RequestContext(
            model=model,
            messages=deepcopy(system + summary_messages + messages + current_turn),
            tools=deepcopy(tools),
            max_output_tokens=self.max_output_tokens,
            estimate=estimate,
            limits=self.limits,
            selected_turn_ids=tuple(turn.turn_id for turn in selected),
            omitted_turn_ids=tuple(turn.turn_id for turn in history[:start]),
            original_history_tokens=original_history_tokens,
            candidate_history_tokens=sum(sizes),
            pruned_tools=tuple(
                item for item in pruned if item["turn_id"] in {turn.turn_id for turn in selected}
            ),
            summary=summary,
        )


def estimate_json_tokens(value: object) -> int:
    """用紧凑 JSON 的 UTF-8 字节数 / 3 向上取整，再加 8 个结构开销估算 token。

    字节数让中文和工具 JSON 都计入长度；该规则不是模型 tokenizer，
    安全余量也不能保证真实请求不超模型窗口。实际消费始终采用服务端 usage。
    """
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (len(serialized.encode("utf-8")) + 2) // 3 + 8

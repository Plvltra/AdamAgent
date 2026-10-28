"""构造模型输入并估算单次预算；不读写会话、不裁剪历史、不请求模型。"""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from adam.config import ContextConfig
from adam.errors import BudgetError, FailureCode


@dataclass(frozen=True)
class ContextEstimate:
    """输入各部分的近似 token 数；与服务端报告的计费用量分别使用。"""

    system_tokens: int
    history_tokens: int
    turn_tokens: int
    tools_tokens: int

    @property
    def input_tokens(self) -> int:
        return self.system_tokens + self.history_tokens + self.turn_tokens + self.tools_tokens


@dataclass(frozen=True)
class RequestContext:
    """一次构造的消息、工具及预算；消息与源历史分离，展示和发送使用同一份内容。"""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_output_tokens: int
    estimate: ContextEstimate
    limits: ContextConfig

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
                f"（超出 {excess} token）；未发送本次请求，未自动裁剪。"
                "请缩短输入、新建会话或调整 [context] 配置。",
                code=FailureCode.CONTEXT_INPUT_LIMIT,
            )


class ContextBuilder:
    """构造系统提示、成功历史及当前轮次；预览和每次模型调用共用，暂无选择/压缩策略。"""

    def __init__(self, limits: ContextConfig, max_output_tokens: int) -> None:
        self.limits = limits
        self.max_output_tokens = max_output_tokens

    def build(
        self,
        *,
        model: str,
        system_prompt: str,
        history: list[dict[str, Any]],
        current_turn: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> RequestContext:
        """返回独立的输入副本及组成估算；保留顺序、工具配对和服务商推理字段。

        history 仅接收已提交成功历史；current_turn 包含最新输入及已产生的工具链，
        预览时为空。预算覆盖整个工具 JSON Schema，不包含认证头、URL 或密钥。
        """
        system = [{"role": "system", "content": system_prompt}] if system_prompt else []
        estimate = ContextEstimate(
            system_tokens=sum(estimate_json_tokens(message) for message in system),
            history_tokens=sum(estimate_json_tokens(message) for message in history),
            turn_tokens=sum(estimate_json_tokens(message) for message in current_turn),
            tools_tokens=estimate_json_tokens(tools) if tools else 0,
        )
        return RequestContext(
            model=model,
            messages=deepcopy(system + history + current_turn),
            tools=deepcopy(tools),
            max_output_tokens=self.max_output_tokens,
            estimate=estimate,
            limits=self.limits,
        )


def estimate_json_tokens(value: object) -> int:
    """用紧凑 JSON 的 UTF-8 字节数 / 3 向上取整，再加 8 个结构开销估算 token。

    字节数让中文和工具 JSON 都计入长度；该规则不是模型 tokenizer，
    安全余量也不能保证真实请求不超模型窗口。实际消费始终采用服务端 usage。
    """
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (len(serialized.encode("utf-8")) + 2) // 3 + 8

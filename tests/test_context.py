"""上下文构造的协议保真、输入预算边界及字符估算性质。"""

from dataclasses import replace

import pytest

from adam.config.settings import ContextConfig
from adam.context.builder import ContextBuilder
from adam.contracts.errors import BudgetError
from adam.contracts.history import HistoryTurn


def build(*, history=None, current_turn=None, tools=None, prompt="system"):
    return ContextBuilder(ContextConfig(), 4096).build(
        model="test",
        system_prompt=prompt,
        history=[HistoryTurn("old", history)] if history else [],
        current_turn=current_turn or [],
        tools=tools or [],
    )


def test_context_is_detached_and_preserves_protocol_fields():
    history = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "reason",
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    current = [{"role": "user", "content": "next"}]
    context = build(history=history, current_turn=current, tools=tools)
    assert context.messages == [{"role": "system", "content": "system"}] + history + current
    assert context.tools == tools
    assert all(
        value > 0
        for value in (
            context.estimate.system_tokens,
            context.estimate.history_tokens,
            context.estimate.turn_tokens,
            context.estimate.tools_tokens,
        )
    )
    history[1]["tool_calls"][0]["function"]["arguments"] = "changed"
    tools[0]["function"]["name"] = "changed"
    current[0]["content"] = "changed"
    assert context.messages[2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert context.messages[-1]["content"] == "next"
    assert context.tools[0]["function"]["name"] == "read"


@pytest.mark.parametrize("excess", [0, 1])
def test_budget_boundary_includes_margin_but_not_output_twice(excess):
    context = build(current_turn=[{"role": "user", "content": "中文"}])
    context = replace(
        context,
        limits=ContextConfig(
            max_input_tokens=context.estimate.input_tokens + 10 - excess,
            safety_margin_tokens=10,
        ),
    )
    assert context.max_output_tokens == 4096  # 输出上限是另外预留的，不当作输入再扣一次。
    if excess:
        with pytest.raises(BudgetError, match="超出 1 token"):
            context.ensure_within_budget()
    else:
        context.ensure_within_budget()


@pytest.mark.parametrize("include", [False, True])
def test_tool_turn_is_selected_whole_at_boundary(include):
    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c", "content": "result"},
        {"role": "assistant", "content": "done", "reasoning_content": "reason"},
    ]
    full = build(history=messages)
    limits = ContextConfig(
        max_input_tokens=full.estimate.input_tokens + 10 - (not include),
        safety_margin_tokens=10,
        retain_recent_turns=0,
    )
    selected = ContextBuilder(limits, 4096).build(
        model="test",
        system_prompt="system",
        history=[HistoryTurn("old", messages)],
        current_turn=[],
        tools=[],
    )
    assert selected.messages == full.messages if include else selected.messages == full.messages[:1]
    assert selected.selected_turn_ids == (("old",) if include else ())


def test_estimate_accounts_for_unicode_and_entire_tool_schema():
    empty = build(prompt="")
    ascii_context = build(prompt="a" * 100)
    chinese = build(prompt="中" * 100)
    assert empty.estimate.input_tokens == 0 and empty.messages == []
    assert chinese.estimate.system_tokens > ascii_context.estimate.system_tokens > 0
    base = build(tools=[{"type": "function", "function": {"name": "read"}}])
    detailed = build(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "描述" * 100,
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    assert detailed.estimate.tools_tokens > base.estimate.tools_tokens
    assert detailed.estimate.system_tokens == base.estimate.system_tokens

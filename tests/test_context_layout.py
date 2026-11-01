"""B03.2：固定材料的前缀对照、预算和完整消息块；不推断服务端缓存命中。"""

from dataclasses import replace
from hashlib import sha256

import pytest

from adam.cli.terminal import show_context
from adam.config.settings import ContextConfig
from adam.context.builder import ContextBuilder
from adam.contracts.errors import BudgetError
from adam.contracts.execution import ExecutionMode
from adam.contracts.history import CONTEXT_LAYOUT_VERSION, HistoryTurn, Summary
from adam.contracts.rules import RuleDocument, RuleSelection
from adam.llm.input_observation import PrefixTracker

CATALOG = ({"id": "reading", "name": "Reading", "description": "Read source", "version": "a" * 64},)


def material():
    """固定历史包含完整工具配对和推理字段，避免只比较纯文本玩具输入。"""
    return [
        HistoryTurn(
            "old",
            [
                {"role": "user", "content": "阅读源码"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "先读文件",
                    "tool_calls": [
                        {
                            "id": "read",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "read", "content": "稳定中文证据" * 100},
                {"role": "assistant", "content": "已阅读"},
            ],
        )
    ]


def build(layout=CONTEXT_LAYOUT_VERSION, **overrides):
    values = dict(
        model="demo",
        system_prompt="稳定约束",
        history=material(),
        current_turn=[{"role": "user", "content": "继续"}],
        tools=[],
        skills=CATALOG,
        runtime_instructions="待收集子任务：one",
        execution_mode=ExecutionMode.ACT,
    )
    values.update(overrides)
    return ContextBuilder(ContextConfig(), 4096, layout_version=layout).build(**values)


def test_fixed_history_preserves_more_prefix_when_only_runtime_state_changes():
    observed = {}
    for layout in (None, CONTEXT_LAYOUT_VERSION):
        tracker = PrefixTracker()
        first = build(layout)
        second = build(layout, runtime_instructions="待收集子任务：two")
        tracker.observe(first.model, first.messages, first.tools)
        observed[layout] = tracker.observe(second.model, second.messages, second.tools)
        assert second.messages[-1] == first.messages[-1]
        assert second.selected_turn_ids == first.selected_turn_ids == ("old",)
    assert (
        observed[CONTEXT_LAYOUT_VERSION].common_prefix_bytes
        > observed[None].common_prefix_bytes + 1000
    )
    assert observed[CONTEXT_LAYOUT_VERSION].layout_version == "chat_completions_input_v1"


def test_catalog_is_stable_prefix_and_runtime_is_reference_after_history(tmp_path):
    text = "修改前验证"
    rules = RuleSelection(
        str(tmp_path), (RuleDocument(".", "AGENTS.md", sha256(text.encode()).hexdigest(), text),)
    )
    history = material()
    context = build(history=history, rules=rules)
    assert context.messages[0]["role"] == "system"
    assert "当前执行模式：act。" in context.messages[0]["content"]
    assert "待收集" not in context.messages[0]["content"]
    assert (
        context.messages[1]["role"] == "user"
        and "当前可用任务指导" in context.messages[1]["content"]
    )
    assert context.messages[2 : 2 + len(history[0].messages)] == history[0].messages
    assert context.messages[-3] == rules.messages()[0]
    assert context.messages[-2]["role"] == "user"
    assert "不是新的用户授权" in context.messages[-2]["content"]
    assert context.selection_record()["layout_version"] == CONTEXT_LAYOUT_VERSION
    assert context.estimate.runtime_tokens > 0
    history[0].messages[2]["content"] = "外部修改"
    assert context.messages[4]["content"] != "外部修改"


def test_legacy_layout_preserves_original_prompt_and_log_shape():
    context = build(None)
    assert context.messages[0]["content"].startswith(
        "稳定约束待收集子任务：one\n\n当前执行模式：act。"
    )
    assert context.messages[1:-2] == material()[0].messages
    assert "当前可用任务指导" in context.messages[-2]["content"]
    assert "layout_version" not in context.selection_record()
    assert context.estimate.runtime_tokens == 0


@pytest.mark.parametrize("layout", [None, CONTEXT_LAYOUT_VERSION])
def test_summary_and_complete_current_tool_chain_keep_original_sources(layout):
    history = material()
    recent = HistoryTurn(
        "recent", [{"role": "user", "content": "recent"}, {"role": "assistant", "content": "done"}]
    )
    current = material()[0].messages[:-1]
    context = build(
        layout,
        history=history + [recent],
        current_turn=current,
        summary=Summary("summary", "旧证据", ("old",)),
    )
    assert context.messages[-len(current) :] == current
    assert context.selected_turn_ids == ("recent",)
    assert context.summary.covered_turn_ids == ("old",)
    assert sum("旧证据" in (m.get("content") or "") for m in context.messages) == 1
    assert sum(m.get("tool_call_id") == "read" for m in context.messages) == 1


def test_runtime_counts_in_protected_budget_and_context_display():
    context = build(history=[], skills=(), runtime_instructions="运行提示" * 200)
    exact = replace(
        context,
        limits=replace(
            context.limits, max_input_tokens=context.estimate.input_tokens, safety_margin_tokens=0
        ),
    )
    exact.ensure_within_budget()
    with pytest.raises(BudgetError):
        replace(
            exact, limits=replace(exact.limits, max_input_tokens=exact.estimate.input_tokens - 1)
        ).ensure_within_budget()
    shown = []
    show_context(exact, shown.append)
    assert any(f"运行提示 {exact.estimate.runtime_tokens}" in line for line in shown)


def test_budget_selection_drops_whole_history_without_dropping_runtime_reminder():
    full = build()
    limits = ContextConfig(
        max_input_tokens=full.estimate.input_tokens - full.estimate.history_tokens,
        safety_margin_tokens=0,
        retain_recent_turns=0,
        prune_tool_results=False,
    )
    context = ContextBuilder(limits, 4096).build(
        model=full.model,
        system_prompt="稳定约束",
        execution_mode=ExecutionMode.ACT,
        history=material(),
        current_turn=[{"role": "user", "content": "继续"}],
        tools=[],
        skills=CATALOG,
        runtime_instructions="待收集子任务：one",
    )
    context.ensure_within_budget()
    assert context.selected_turn_ids == () and context.omitted_turn_ids == ("old",)
    assert not any(m["role"] in {"assistant", "tool"} for m in context.messages)
    assert context.messages[-2] == full.messages[-2]


@pytest.mark.parametrize("value", ["future_layout", False, 1, {}])
def test_unknown_layout_is_rejected_before_build(value):
    with pytest.raises(ValueError, match="context layout"):
        ContextBuilder(ContextConfig(), 4096, layout_version=value)


def test_changed_mode_changes_stable_prefix_without_hiding_current_permissions():
    act = build(execution_mode=ExecutionMode.ACT)
    plan = build(execution_mode=ExecutionMode.PLAN)
    assert act.messages[0] != plan.messages[0]
    assert "当前执行模式：plan。" in plan.messages[0]["content"]
    assert act.messages[1:] == plan.messages[1:]

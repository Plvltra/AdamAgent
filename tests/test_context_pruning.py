"""旧工具结果只改变模型视图，保留状态、来源与受保护的近期内容。"""

import json

import pytest

from adam.config.settings import ContextConfig
from adam.context.builder import ContextBuilder
from adam.context.pruning import prune_history
from adam.contracts.history import HistoryTurn


def history():
    output = json.dumps(
        {"ok": True, "exit_code": 1, "stderr": "failed detail", "stdout": "中" * 6000}
    )
    messages = [
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c", "content": output},
        {"role": "assistant", "content": "done"},
    ]
    return [HistoryTurn(str(index), messages) for index in range(3)]


def test_pruning_preserves_status_pairing_and_recent_turns():
    turns = history()
    changed, records = prune_history(turns, ContextConfig())
    body = json.loads(changed[0].messages[2]["content"])
    assert body["ok"] and body["exit_code"] == 1 and body["stderr"] == "failed detail"
    assert body["context_pruned"] and body["source"] == {"turn_id": "0", "tool_call_id": "c"}
    assert len(body["excerpt"]) < 2100
    assert changed[0].messages[1] == turns[0].messages[1]
    assert changed[0].messages[2]["tool_call_id"] == "c"
    assert changed[1:] == turns[1:] and changed[0] != turns[0]
    assert len(records) == 1 and records[0]["result_chars"] < records[0]["original_chars"]
    repeated, _ = prune_history(changed, ContextConfig())
    assert repeated == changed  # 不反复给节选套标记。


@pytest.mark.parametrize("enabled,pressure", [(False, True), (True, False), (True, True)])
def test_pruning_runs_only_when_enabled_and_under_pressure(enabled, pressure):
    turns = history()
    limits = ContextConfig(
        max_input_tokens=1000000 if not pressure else 30000,
        safety_margin_tokens=0,
        prune_tool_results=enabled,
    )
    # JSON 默认转义中文，原始正文约 36k 字符，确保启用压力分支。
    current = [{"role": "user", "content": "current input"}]
    context = ContextBuilder(limits, 4096).build(
        model="test", system_prompt="system", history=turns, current_turn=current, tools=[]
    )
    assert bool(context.pruned_tools) == (enabled and pressure)
    assert context.messages[-1] == current[0]
    assert context.original_history_tokens >= context.candidate_history_tokens
    assert turns == history()


def test_large_error_metadata_is_not_discarded_for_size_target():
    raw = json.dumps({"ok": False, "error": "failure" * 2000})
    turns = [HistoryTurn("old", [{"role": "tool", "tool_call_id": "c", "content": raw}])]
    changed, records = prune_history(turns, ContextConfig(retain_recent_turns=0))
    assert changed == turns and records == ()

"""记忆选择的配置、独立输入上限与持久化契约边界。"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from adam.application.memories import MemoryService
from adam.config.settings import ContextConfig, MemoryConfig
from adam.context.builder import ContextBuilder, estimate_json_tokens
from adam.contracts.errors import ConfigError
from adam.contracts.history import HistoryTurn
from adam.contracts.memory import MemorySource, validate_selection
from adam.memory.retrieval import rank_records
from adam.memory.store import MemoryStore


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"max_entries": True},
        {"max_entries": 0},
        {"max_input_tokens": -1},
        {"max_input_tokens": True},
    ],
)
def test_memory_config_rejects_invalid_types_and_limits(values):
    with pytest.raises(ConfigError):
        MemoryConfig(**values)


def test_selection_keeps_whole_records_within_both_budgets(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    first = store.add("Python import " * 50, None, MemorySource("add"))
    second = store.add("Python import short", None, MemorySource("add"))
    service = MemoryService(store, tmp_path, MemoryConfig(max_entries=1))
    selected = service.select("Python import", 500)
    assert selected.records == (second,)
    assert sum(estimate_json_tokens(message) for message in selected.messages()) <= 500
    assert first not in selected.records
    validate_selection(selected.to_dict())


def test_total_budget_reserves_recent_history_before_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.add("Python 绝对导入", None, MemorySource("add"))
    service = MemoryService(store, tmp_path)
    history = [
        HistoryTurn(
            "a",
            [
                {"role": "user", "content": "protected question"},
                {"role": "assistant", "content": "protected answer"},
            ],
        )
    ]
    query = [{"role": "user", "content": "Python 导入"}]
    generous = ContextBuilder(ContextConfig(auto_compact=False), 100).build(
        model="test", system_prompt="required", history=history, current_turn=query, tools=[]
    )
    limit = generous.estimate.input_tokens + 2
    builder = ContextBuilder(
        ContextConfig(max_input_tokens=limit, safety_margin_tokens=1, auto_compact=False), 100
    )
    base = builder.build(
        model="test", system_prompt="required", history=history, current_turn=query, tools=[]
    )
    memory = service.select("Python 导入", base.memory_capacity)
    final = builder.build(
        model="test",
        system_prompt="required",
        history=history,
        current_turn=query,
        tools=[],
        memory=memory,
    )
    assert not memory.records and final.selected_turn_ids == ("a",)
    assert final.messages == base.messages
    final.ensure_within_budget()


def test_chinese_keyword_matching_and_expiry(tmp_path):
    store = MemoryStore(tmp_path)
    relevant = store.add("编码规范要求绝对导入", None, MemorySource("add"))
    store.add("午餐面条", None, MemorySource("add"))
    store.add("绝对导入旧要求", None, MemorySource("add"), expires_at="2020-01-01T00:00:00Z")
    assert rank_records(store.list_records(None), "解释导入规范", datetime.now(UTC)) == [relevant]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["records"][0].update(revision=True),
        lambda data: data["records"][0].update(deleted=True),
        lambda data: data.update(status="no_match"),
        lambda data: data["records"].append(deepcopy(data["records"][0])),
        lambda data: data.update(strategy="unknown"),
    ],
)
def test_invalid_request_memory_snapshot_is_rejected(tmp_path, mutation):
    store = MemoryStore(tmp_path / "memory")
    store.add("Python import", None, MemorySource("add"))
    data = MemoryService(store, tmp_path).select("Python", 1000).to_dict()
    mutation(data)
    with pytest.raises(ValueError):
        validate_selection(data)

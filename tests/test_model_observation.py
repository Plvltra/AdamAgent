"""缓存未知与计费分离、逻辑 UTF-8 前缀和本地记录校验；不请求模型。"""

import json
from hashlib import sha256

import pytest

from adam.contracts.usage import CacheUsage, decode_cache_usage, decode_input_prefix
from adam.llm.input_observation import PrefixTracker
from adam.llm.usage import parse_cache_usage, parse_usage


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({}, None),
        ({"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100}, (0, 100)),
        ({"prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60}, (40, 60)),
        ({"prompt_cache_hit_tokens": 40}, (40, None)),
        ({"prompt_cache_miss_tokens": 60}, (None, 60)),
        ({"prompt_tokens_details": {"cached_tokens": 40}}, (40, None)),
        (
            {"prompt_cache_hit_tokens": None, "prompt_tokens_details": {"cached_tokens": 40}},
            (40, None),
        ),
        (
            {
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 60,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
            (40, 60),
        ),
        ({"prompt_cache_hit_tokens": 40, "prompt_tokens_details": {"cached_tokens": 41}}, None),
        ({"prompt_cache_hit_tokens": True}, None),
        ({"prompt_cache_miss_tokens": -1}, None),
        ({"prompt_tokens_details": {"cached_tokens": 1.0}}, None),
        ({"prompt_cache_hit_tokens": "40"}, None),
        ({"prompt_cache_hit_tokens": 101}, None),
        ({"prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 61}, None),
    ],
)
def test_cache_fields_are_reported_not_inferred_and_bad_details_do_not_erase_total(extra, expected):
    payload = {"prompt_tokens": 100, "completion_tokens": 7, **extra}
    result = parse_cache_usage(payload)
    assert parse_usage(payload) == (100, 7)
    assert (None if result is None else (result.hit_tokens, result.miss_tokens)) == expected
    if result:
        assert decode_cache_usage(result.to_record(), 100) == result


@pytest.mark.parametrize(
    "usage", [{}, {"prompt_tokens": True, "completion_tokens": 1}, {"prompt_tokens": 4}]
)
def test_cache_cannot_make_unknown_total_reliable(usage):
    payload = {**usage, "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 2}
    assert parse_usage(payload) is None and parse_cache_usage(payload) is None


def test_common_prefix_counts_utf8_bytes_with_explicit_layout_and_no_original_text_in_record():
    tracker = PrefixTracker()
    first = tracker.observe("demo", [{"role": "user", "content": "中A"}], [])
    second = tracker.observe("demo", [{"role": "user", "content": "中B"}], [])
    expected = (
        '[{"model":"demo"},{"tools":[]},{"messages":[{"content":"中A","role":"user"}]}]'.encode()
    )
    assert first.input_bytes == len(expected)
    assert first.input_sha256 == sha256(expected).hexdigest()
    assert first.common_prefix_bytes is None and first.previous_sequence is None
    assert second.common_prefix_bytes == len(
        '[{"model":"demo"},{"tools":[]},{"messages":[{"content":"中'.encode()
    )
    assert second.previous_sha256 == first.input_sha256
    assert decode_input_prefix(second.to_record()) == second
    assert "中A" not in json.dumps(first.to_record(), ensure_ascii=False)


def test_dict_key_order_is_stable_but_tool_and_message_array_order_are_significant():
    tracker = PrefixTracker()
    tools = [{"name": "first"}, {"name": "second"}]
    first = tracker.observe("demo", [{"role": "user", "content": "question"}], tools)
    same = tracker.observe("demo", [{"content": "question", "role": "user"}], tools)
    assert same.input_sha256 == first.input_sha256
    assert same.common_prefix_bytes == same.input_bytes
    changed = tracker.observe(
        "demo", [{"content": "question", "role": "user"}], list(reversed(tools))
    )
    assert changed.input_sha256 != same.input_sha256
    assert changed.common_prefix_bytes < changed.input_bytes
    other = tracker.observe(
        "demo", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], tools
    )
    reordered = tracker.observe(
        "demo", [{"role": "assistant", "content": "b"}, {"role": "user", "content": "a"}], tools
    )
    assert other.input_sha256 != reordered.input_sha256


def test_model_switch_and_new_client_start_without_guessing_previous_input():
    tracker = PrefixTracker()
    messages = [{"role": "user", "content": "question"}]
    first = tracker.observe("one", messages, None)
    switched = tracker.observe("two", messages, None)
    returned = tracker.observe("one", messages, None)
    restarted = PrefixTracker().observe("one", messages, None)
    assert switched.previous_sequence is None and returned.previous_sequence is None
    assert restarted.input_sha256 == first.input_sha256 and restarted.common_prefix_bytes is None
    assert restarted.scope_id != first.scope_id
    assert returned.sequence == 3


def test_invalid_input_does_not_poison_next_observation():
    tracker = PrefixTracker()
    messages = [{"role": "user", "content": "question"}]
    first = tracker.observe("demo", messages, None)
    with pytest.raises(ValueError):
        tracker.observe("demo", [{"value": float("nan")}], None)
    second = tracker.observe("demo", messages, None)
    assert second.sequence == 2 and second.previous_sequence == first.sequence


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_layout",
        "boolean_sequence",
        "excess_prefix",
        "partial_previous",
        "hash_disagrees",
        "extra",
    ],
)
def test_corrupt_local_input_observation_is_rejected(damage):
    tracker = PrefixTracker()
    messages = [{"role": "user", "content": "question"}]
    tracker.observe("demo", messages, None)
    record = tracker.observe("demo", messages, None).to_record()
    if damage == "unknown_layout":
        record["layout_version"] = "future"
    elif damage == "boolean_sequence":
        record["sequence"] = True
    elif damage == "excess_prefix":
        record["common_prefix_bytes"] += 1
    elif damage == "partial_previous":
        record["previous_sha256"] = None
    elif damage == "hash_disagrees":
        record["input_sha256"] = "f" * 64
    else:
        record["raw_input"] = "not allowed"
    with pytest.raises(ValueError):
        decode_input_prefix(record)


def test_old_records_are_unknown_and_inconsistent_local_cache_is_not_silently_discarded():
    assert decode_cache_usage(None, None) is None
    assert decode_input_prefix(None) is None
    with pytest.raises(ValueError):
        decode_cache_usage(CacheUsage(1, 2).to_record(), 4)
    with pytest.raises(ValueError):
        decode_cache_usage(CacheUsage(1, None).to_record(), None)

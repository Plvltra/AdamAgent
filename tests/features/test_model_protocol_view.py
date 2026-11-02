"""公共消息视图保留工具身份、原始推理和独立副本；不借此声称协议已经接通。"""

from copy import deepcopy

import pytest

from adam.contracts.model_history import message_view, protocol_capabilities


def test_tool_chain_has_stable_identity_and_round_trips_original_fields():
    messages = [
        {"role": "user", "content": "读取文件"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "private reasoning",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "real result"},
        {"role": "assistant", "content": "done"},
    ]
    views = [message_view(message) for message in messages]
    assert views[1].calls[0].id == views[2].reply_to == "call_1"
    assert views[1].calls[0].name == "read_file"
    assert views[1].extensions == {"reasoning_content": "private reasoning"}
    assert [view.to_raw() for view in views] == messages


def test_provider_extensions_survive_without_mutating_original_history():
    raw = {"role": "assistant", "content": "answer", "vendor_data": {"signature": "opaque"}}
    before = deepcopy(raw)
    view = message_view(raw)
    raw["vendor_data"]["signature"] = "changed"
    output = view.to_raw()
    assert output == before
    output["vendor_data"]["signature"] = "another change"
    assert view.to_raw() == before


@pytest.mark.parametrize(
    "raw",
    [
        {"role": "developer", "content": "unsupported"},
        {"role": "user", "content": [{"type": "image"}]},
        {"role": "tool", "content": "missing identity"},
    ],
)
def test_unsupported_content_is_rejected_instead_of_silently_flattened(raw):
    with pytest.raises(ValueError):
        message_view(raw)


def test_capabilities_describe_minimal_adapter_support():
    assert protocol_capabilities("chat_completions").streaming
    assert protocol_capabilities("chat_completions").reasoning_field == "reasoning_content"
    assert not protocol_capabilities("messages").streaming
    assert protocol_capabilities("messages").tools
    with pytest.raises(ValueError):
        protocol_capabilities("unknown")

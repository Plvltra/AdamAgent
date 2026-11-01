"""C00.3 公共消息/能力/供应商编码边界，不调用真实视觉模型。"""

import base64
import hashlib
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from adam.context.builder import estimate_json_tokens
from adam.contracts.errors import AgentError, FailureCode, LLMError
from adam.contracts.media import MediaContent, MediaRef, MediaSource
from adam.contracts.model_input import (
    MEDIA_INPUT_RESERVE,
    MediaPart,
    MessageView,
    ModelCapabilities,
    TextPart,
    declared_capabilities,
    media_records,
    message_media,
)
from adam.contracts.protocol import validate_turn
from adam.llm.message_encoding import ChatInputEncoder


def reference(data=b"bounded bytes", **changes):
    """编码层只核对身份/摘要，不冒充 C04 的解码器。"""
    ref = MediaRef(
        "c" * 32,
        MediaSource("a" * 8, "b" * 32, "input"),
        "image/png",
        len(data),
        hashlib.sha256(data).hexdigest(),
        "2026-11-01T00:00:00+00:00",
    )
    return replace(ref, **changes)


def test_public_view_and_wire_encoding_keep_order_identity_reasoning_and_original():
    data = b"bounded bytes"
    first, second = reference(), reference(media_id="d" * 32)
    messages = [
        {"role": "system", "content": "constraint"},
        {"role": "user", "content": "question", "media": media_records((first, second))},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "vendor reasoning",
            "tool_calls": [
                {
                    "id": "identity",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"x":1}'},
                }
            ],
        },
        {"role": "tool", "content": '{"ok":true}', "tool_call_id": "identity"},
    ]
    original = deepcopy(messages)
    view = MessageView.from_message(messages[1])
    assert view.parts == (TextPart("question"), MediaPart(first), MediaPart(second))
    assistant = MessageView.from_message(messages[2])
    assert assistant.tool_calls[0].call_id == "identity"
    assert assistant.tool_calls[0].arguments == '{"x":1}'
    assert assistant.reasoning == "vendor reasoning"
    assert MessageView.from_message(messages[3]).tool_call_id == "identity"
    encoder = ChatInputEncoder(
        ModelCapabilities("vision", ("image/png",)), lambda ref: MediaContent(ref, data)
    )
    wire = encoder.encode(messages, model="vision", has_tools=True, stream=True)
    assert wire[0] == messages[0] and wire[2:] == messages[2:]
    assert wire[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "question"},
            *[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(data).decode()
                    },
                }
                for _ in range(2)
            ],
        ],
    }
    assert messages == original
    validate_turn([*messages[1:], {"role": "assistant", "content": "done"}])


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "text", "image_url": "https://untrusted.test"},
        {"role": "user", "content": [{"type": "text", "text": "not canonical"}]},
        {"role": "developer", "content": "unsupported role"},
        {"role": "assistant", "content": "answer", "opaque_vendor": 1},
        {"role": "assistant", "content": "answer", "media": []},
        {"role": "tool", "content": "result"},
        {"role": "assistant", "content": "answer", "reasoning_content": {}},
        {"role": "assistant", "content": "answer", "tool_calls": False},
        {"role": "assistant", "content": "answer", "tool_calls": ""},
        {"role": "assistant", "content": "answer", "tool_calls": {}},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "duplicate",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                },
                {
                    "id": "duplicate",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "x",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}", "vendor": 1},
                }
            ],
        },
    ],
)
def test_nonportable_fields_and_provider_binary_are_explicitly_rejected(message):
    with pytest.raises(ValueError):
        MessageView.from_message(message)
    with pytest.raises(LLMError) as failure:
        ChatInputEncoder(ModelCapabilities("text")).encode(
            [message], model="text", has_tools=False, stream=False
        )
    assert failure.value.code == FailureCode.MODEL_INPUT_INVALID and failure.value.usage == (0, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_media_types": ["image/png"]},
        {"input_media_types": ({},)},
        {"input_media_types": ("image/png", "image/png")},
        {"input_media_types": ("audio/wav",)},
        {"input_media_types": ("image/*",)},
        {"max_media_parts": True},
        {"max_media_parts": 0},
        {"max_media_parts": 17},
        {"max_media_bytes": 0},
        {"max_media_bytes": 100000001},
        {"tools": 1},
        {"streaming": None},
        {"reasoning": "true"},
        {"model": " "},
    ],
)
def test_capability_declaration_is_strict(changes):
    with pytest.raises(ValueError):
        ModelCapabilities(**({"model": "vision"} | changes))


@pytest.mark.parametrize(
    "capability,model,refs",
    [
        (ModelCapabilities("vision"), "vision", (reference(),)),
        (ModelCapabilities("vision", ("image/png",)), "changed-model", (reference(),)),
        (
            ModelCapabilities("vision", ("image/png",)),
            "vision",
            (reference(mime_type="audio/wav"),),
        ),
        (
            ModelCapabilities("vision", ("image/png",), max_media_parts=1),
            "vision",
            (reference(), reference()),
        ),
        (ModelCapabilities("vision", ("image/png",), max_media_bytes=1), "vision", (reference(),)),
    ],
)
def test_unsupported_media_is_rejected_before_reader(capability, model, refs):
    reads = []
    encoder = ChatInputEncoder(capability, lambda ref: reads.append(ref))
    with pytest.raises(LLMError):
        encoder.encode(
            [{"role": "user", "content": "q", "media": media_records(refs)}],
            model=model,
            has_tools=False,
            stream=False,
        )
    assert reads == []


@pytest.mark.parametrize(
    "content",
    [
        None,
        MediaContent(reference(), b"corrupt"),
        MediaContent(reference(media_id="d" * 32), b"bounded bytes"),
    ],
)
def test_reader_must_return_exact_reference_and_full_bytes(content):
    encoder = ChatInputEncoder(ModelCapabilities("vision", ("image/png",)), lambda _: content)
    with pytest.raises(LLMError) as error:
        encoder.encode(
            [{"role": "user", "content": "q", "media": media_records((reference(),))}],
            model="vision",
            has_tools=False,
            stream=False,
        )
    assert error.value.usage == (0, 0)


@pytest.mark.parametrize(
    "flag,tools,stream,message",
    [
        ("tools", True, False, {"role": "user", "content": "q"}),
        ("streaming", False, True, {"role": "user", "content": "q"}),
        (
            "reasoning",
            False,
            False,
            {"role": "assistant", "content": "a", "reasoning_content": "r"},
        ),
    ],
)
def test_unavailable_nonmedia_capability_cannot_be_silently_dropped(flag, tools, stream, message):
    with pytest.raises(LLMError):
        ChatInputEncoder(ModelCapabilities(**{"model": "text", flag: False})).encode(
            [message], model="text", has_tools=tools, stream=stream
        )


@pytest.mark.parametrize(
    "media",
    [[], ["path.png"], [asdict(reference())] * 17, [{"url": "https://untrusted.test/image"}]],
)
def test_canonical_media_is_bounded_and_reference_only(media):
    with pytest.raises(ValueError):
        message_media({"role": "user", "content": "q", "media": media})


def test_legacy_client_defaults_text_only_and_media_budget_is_not_reference_json_only():
    assert declared_capabilities(object(), "legacy").input_media_types == ()
    ref = reference()
    text = {"role": "user", "content": "q"}
    media = {**text, "media": media_records((ref,))}
    assert estimate_json_tokens(media) - estimate_json_tokens(text) >= MEDIA_INPUT_RESERVE
    with pytest.raises(AgentError):
        declared_capabilities(object(), "legacy").require_media((ref,), "legacy")

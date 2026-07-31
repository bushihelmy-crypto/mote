import pytest

from mote.product.models.transports.anthropic import _anthropic_canonical_messages
from mote.product.models.transports.google import _google_canonical_contents
from mote.product.models.transports.translation import (
    translate_anthropic_message,
    translate_anthropic_stream,
    translate_google_generate_content,
    translate_openai_chat,
    translate_openai_chat_stream,
    translate_openai_responses,
)


def test_openai_chat_translation_preserves_tools_cache_and_reasoning_usage():
    response = translate_openai_chat(
        {
            "id": "provider-request",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 6},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
    )
    assert response.output.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage.cache_read_tokens == 6
    assert response.usage.reasoning_tokens == 2


def test_openai_stream_translation_reassembles_content_and_tool_arguments():
    response = translate_openai_chat_stream(
        (
            {
                "id": "request",
                "choices": [
                    {
                        "delta": {
                            "content": "hello ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call",
                                    "function": {
                                        "name": "Read",
                                        "arguments": '{"path":',
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "world",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"file"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 9},
            },
        )
    )
    assert response.output.content == "hello world"
    assert response.output.tool_calls[0].name == "Read"
    assert response.output.tool_calls[0].arguments == {"path": "file"}
    assert response.usage.total_tokens == 9


def test_openai_translation_rejects_ambiguous_multi_choice():
    with pytest.raises(ValueError, match="exactly one"):
        translate_openai_chat({"choices": [{"message": {}}, {"message": {}}]})


def test_openai_translation_rejects_non_object_tool_arguments():
    with pytest.raises(ValueError, match="arguments must be an object"):
        translate_openai_chat(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {"name": "Read", "arguments": "[]"},
                                }
                            ]
                        }
                    }
                ]
            }
        )


def test_responses_translation_preserves_text_tools_and_usage():
    response = translate_openai_responses(
        {
            "id": "response",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call",
                    "name": "Read",
                    "arguments": '{"path":"a"}',
                },
            ],
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "input_tokens_details": {"cached_tokens": 1},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
        }
    )
    assert response.output.content == "done"
    assert response.output.tool_calls[0].arguments == {"path": "a"}
    assert response.usage.cache_read_tokens == 1


def test_anthropic_unary_and_stream_translation_are_equivalent():
    unary = translate_anthropic_message(
        {
            "id": "message",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": "tool",
                    "name": "Read",
                    "input": {"path": "a"},
                },
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            },
        }
    )
    streamed = translate_anthropic_stream(
        (
            {
                "type": "message_start",
                "message": {
                    "id": "message",
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 1,
                    },
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"a"}',
                },
            },
            {"type": "message_delta", "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        )
    )
    assert streamed == unary


def test_google_translation_preserves_function_calls_and_usage():
    response = translate_google_generate_content(
        (
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "answer"},
                                {
                                    "functionCall": {
                                        "name": "Read",
                                        "args": {"path": "a"},
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 7,
                    "cachedContentTokenCount": 2,
                    "thoughtsTokenCount": 1,
                },
            },
        )
    )
    assert response.output.content == "answer"
    assert response.output.tool_calls[0].arguments == {"path": "a"}
    assert response.usage.cache_read_tokens == 2
    assert response.usage.reasoning_tokens == 1


def test_anthropic_request_translation_preserves_tool_history_and_multimodal():
    messages = _anthropic_canonical_messages(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "opaque",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "Read",
                            "arguments": {"path": "a"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "Read",
                    "tool_call_id": "call-1",
                    "content": "result",
                },
            ]
        }
    )
    assert messages[0]["content"][1]["type"] == "image"
    assert messages[1]["content"][0] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "Read",
        "input": {"path": "a"},
    }
    assert messages[2]["content"][0]["tool_use_id"] == "call-1"


def test_google_request_translation_preserves_tool_history_and_multimodal():
    contents = _google_canonical_contents(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": "inspect"},
                        {"inlineData": {"mimeType": "image/png", "data": "opaque"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-1", "name": "Read", "arguments": {"path": "a"}}],
                },
                {"role": "tool", "name": "Read", "content": {"text": "result"}},
            ]
        }
    )
    assert contents[0]["parts"][1]["inlineData"]["mimeType"] == "image/png"
    assert contents[1]["parts"][0]["functionCall"]["args"] == {"path": "a"}
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "Read",
        "response": {"text": "result"},
    }

"""Projection from canonical conversation messages to model-provider wire data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from mote.contracts.conversation import Message
from mote.contracts.conversation.fields import CACHE_INTENT, TOOL_CALL_ID, TOOL_CALLS, TOOL_REFERENCES
from mote.contracts.events.envelope import freeze_json
from mote.contracts.model.invocation import CanonicalMessage, CanonicalToolCall
from mote.contracts.tool.calls import serialize_tool_call_args


def _optional_wire_text(value: object, *, field_name: str) -> str | None:
    if value is not None and type(value) is not str:
        raise ValueError(f"model message {field_name} must be a string or null")
    return value


def message_to_model_wire(message: Message) -> dict[str, object]:
    if message.metadata.get(TOOL_CALL_ID):
        wire: dict[str, object] = {
            "role": "tool",
            "tool_call_id": message.metadata[TOOL_CALL_ID],
            "content": message.content,
        }
        references = message.metadata.get(TOOL_REFERENCES)
        if references:
            wire["_tool_references"] = references
    elif message.metadata.get(TOOL_CALLS):
        wire = {
            "role": message.role,
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": serialize_tool_call_args(call.get("args")),
                    },
                }
                for call in message.metadata[TOOL_CALLS]
            ],
        }
    else:
        wire = {"role": message.role, "content": message.content}
    cache_intent = message.metadata.get(CACHE_INTENT)
    if cache_intent:
        wire["_cache_intent"] = cache_intent
    return wire


def canonical_message_from_model_wire(wire: Mapping[str, object]) -> CanonicalMessage:
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls", "_tool_references", "_cache_intent"}
    if not set(wire).issubset(allowed):
        raise ValueError("model message wire contains unknown fields")
    role = wire.get("role")
    if type(role) is not str or not role:
        raise ValueError("model message role must be a non-empty string")
    calls_value = wire.get("tool_calls", [])
    if not isinstance(calls_value, list):
        raise ValueError("model message tool_calls must be an array")
    calls: list[CanonicalToolCall] = []
    for call in calls_value:
        if type(call) is not dict or set(call) != {"id", "type", "function"} or call["type"] != "function":
            raise ValueError("model message tool call shape is invalid")
        function = call["function"]
        if type(function) is not dict or set(function) != {"name", "arguments"}:
            raise ValueError("model message tool function shape is invalid")
        arguments = function["arguments"]
        if type(arguments) is str:
            arguments = json.loads(arguments)
        if type(arguments) is not dict:
            raise ValueError("model message tool arguments must be an object")
        call_id = call["id"]
        name = function["name"]
        if type(call_id) is not str or type(name) is not str or not name:
            raise ValueError("model message tool identity is invalid")
        calls.append(CanonicalToolCall(id=call_id, name=name, arguments=arguments))
    references = wire.get("_tool_references", [])
    if not isinstance(references, list) or any(type(item) is not str for item in references):
        raise ValueError("model message tool references must be an array of strings")
    content = freeze_json(wire.get("content"), path="model message content")
    name_value = _optional_wire_text(wire.get("name"), field_name="name")
    tool_call_id = _optional_wire_text(wire.get("tool_call_id"), field_name="tool_call_id")
    cache_intent = _optional_wire_text(wire.get("_cache_intent"), field_name="cache_intent")
    return CanonicalMessage(
        role=role,
        content=content,
        name=name_value,
        tool_call_id=tool_call_id,
        tool_calls=tuple(calls),
        tool_references=tuple(references),
        cache_intent=cache_intent,
    )


def canonical_messages_from_model_wire(messages: Sequence[Mapping[str, object]]) -> tuple[CanonicalMessage, ...]:
    return tuple(canonical_message_from_model_wire(message) for message in messages)


__all__ = ["canonical_message_from_model_wire", "canonical_messages_from_model_wire", "message_to_model_wire"]

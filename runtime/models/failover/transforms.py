"""Provider-neutral request transforms applied inside one model-call ledger."""

from __future__ import annotations

import json

from mote.contracts.conversation.fields import CACHE_INTENT
from mote.contracts.model.failover import EndpointDescriptor, FailureDisposition, RequestTransform
from mote.contracts.model.invocation import CanonicalMessage, CanonicalToolCall, GenerateInput, ModelInvocation
from mote.contracts.ports.conversation.context_reducer import ContextReducer
from mote.runtime.models.clients.transformers import downgrade_tool_content, shrink_image, strip_request_state

_DEFAULT_CONTEXT_TOKENS = 128_000


class CanonicalRequestTransformer:
    """Apply one transform without retaining mutable state between calls."""

    def __init__(self, context_reducer: ContextReducer | None = None) -> None:
        self._context_reducer = context_reducer

    async def transform(
        self,
        invocation: ModelInvocation,
        transform: RequestTransform,
        disposition: FailureDisposition,
        endpoint: EndpointDescriptor,
    ) -> ModelInvocation | None:
        request = invocation.input
        if not isinstance(request, GenerateInput):
            return None
        messages = [_message_to_wire(message) for message in request.messages]
        if transform is RequestTransform.COMPRESS:
            reducer = self._context_reducer
            if reducer is None:
                return None
            context_tokens = endpoint.capabilities.context_tokens or _DEFAULT_CONTEXT_TOKENS
            transformed = await reducer.reduce(
                messages,
                target_tokens=max(int(context_tokens * 0.8), 1),
            )
        else:
            handler = {
                RequestTransform.SHRINK_IMAGE: shrink_image,
                RequestTransform.DOWNGRADE_TOOL_CONTENT: downgrade_tool_content,
                RequestTransform.STRIP_REQUEST_STATE: strip_request_state,
            }.get(transform)
            if handler is None:
                return None
            transformed = await handler(
                messages,
                RuntimeError(disposition.reason.value),
            )
        if transformed is None:
            return None
        canonical = tuple(_wire_to_message(message) for message in transformed)
        changed_input = request.model_copy(update={"messages": canonical})
        changed = invocation.model_copy(update={"input": changed_input})
        return changed if changed != invocation else None


def _message_to_wire(message: CanonicalMessage) -> dict:
    wire: dict = {
        "role": message.role,
        "content": message.content,
    }
    if message.name is not None:
        wire["name"] = message.name
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_references:
        wire["_tool_references"] = list(message.tool_references)
    if message.cache_intent is not None:
        wire["_cache_intent"] = message.cache_intent
    return wire


def _wire_to_message(wire: dict) -> CanonicalMessage:
    calls: list[CanonicalToolCall] = []
    for value in wire.get("tool_calls") or ():
        function = value.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(
            CanonicalToolCall(
                id=value.get("id", ""),
                name=function.get("name", ""),
                arguments=arguments,
            )
        )
    return CanonicalMessage(
        role=wire.get("role", "user"),
        content=wire.get("content"),
        name=wire.get("name"),
        tool_call_id=wire.get("tool_call_id"),
        tool_calls=tuple(calls),
        tool_references=tuple(wire.get("_tool_references") or ()),
        cache_intent=wire.get("_cache_intent") or wire.get(CACHE_INTENT),
    )


__all__ = ["CanonicalRequestTransformer"]

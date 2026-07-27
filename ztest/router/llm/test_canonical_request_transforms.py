from __future__ import annotations

import pytest

from mote.contracts.models.failover import (
    EndpointCapabilities,
    EndpointDescriptor,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    RequestTransform,
    Retryability,
)
from mote.contracts.models.invocation import (
    CanonicalMessage,
    CanonicalToolCall,
    GenerateInput,
    ModelInvocation,
    ModelOperation,
)
from mote.runtime.models.failover.transforms import CanonicalRequestTransformer


def _endpoint() -> EndpointDescriptor:
    return EndpointDescriptor(
        endpoint_id="primary",
        transport="openai",
        provider="openai",
        model="model",
        base_url_identity="https://example.test",
        capabilities=EndpointCapabilities(context_tokens=100_000),
        credential_pool_id="pool",
        lifecycle_revision="revision",
    )


def _disposition(reason: FailureReason) -> FailureDisposition:
    return FailureDisposition(
        reason=reason,
        domain=FailureDomain.REQUEST,
        retryability=Retryability.AFTER_CHANGE,
        health_verdict=HealthVerdict.NEUTRAL,
    )


def _invocation(messages: tuple[CanonicalMessage, ...]) -> ModelInvocation:
    return ModelInvocation(
        model_call_id="call",
        route_id="default",
        task="interactive",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=messages),
    )


@pytest.mark.asyncio
async def test_compress_uses_endpoint_target_and_preserves_canonical_metadata() -> None:
    class Reducer:
        def __init__(self) -> None:
            self.target_tokens = 0

        async def reduce(self, messages, *, target_tokens):
            self.target_tokens = target_tokens
            return messages[1:]

    reducer = Reducer()
    invocation = _invocation(
        (
            CanonicalMessage(role="user", content="drop"),
            CanonicalMessage(
                role="assistant",
                content="",
                tool_calls=(CanonicalToolCall(id="call-1", name="Read", arguments={}),),
            ),
            CanonicalMessage(
                role="tool",
                content="result",
                tool_call_id="call-1",
                tool_references=("Read",),
                cache_intent="durable",
            ),
        )
    )

    transformed = await CanonicalRequestTransformer(reducer).transform(
        invocation,
        RequestTransform.COMPRESS,
        _disposition(FailureReason.CONTEXT_EXCEEDED),
        _endpoint(),
    )

    assert transformed is not None
    assert reducer.target_tokens == 80_000
    assert transformed.input.messages[0].tool_calls[0].name == "Read"
    assert transformed.input.messages[1].tool_references == ("Read",)
    assert transformed.input.messages[1].cache_intent == "durable"
    assert invocation.input.messages[0].content == "drop"


@pytest.mark.asyncio
async def test_downgrade_tool_content_returns_new_immutable_invocation() -> None:
    invocation = _invocation(
        (
            CanonicalMessage(
                role="tool",
                tool_call_id="call-1",
                content=[
                    {"type": "text", "text": "kept"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            ),
        )
    )

    transformed = await CanonicalRequestTransformer().transform(
        invocation,
        RequestTransform.DOWNGRADE_TOOL_CONTENT,
        _disposition(FailureReason.PROTOCOL_INCOMPATIBLE),
        _endpoint(),
    )

    assert transformed is not None
    assert transformed.input.messages[0].content == "kept"
    assert isinstance(invocation.input.messages[0].content, list)


@pytest.mark.asyncio
async def test_transform_without_progress_returns_none() -> None:
    invocation = _invocation((CanonicalMessage(role="user", content="clean"),))

    transformed = await CanonicalRequestTransformer().transform(
        invocation,
        RequestTransform.STRIP_REQUEST_STATE,
        _disposition(FailureReason.PROTOCOL_INCOMPATIBLE),
        _endpoint(),
    )

    assert transformed is None

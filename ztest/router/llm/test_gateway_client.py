from __future__ import annotations

import hashlib

import pytest

from mote.contracts.artifact import ArtifactRef
from mote.contracts.conversation import AIMessage, ToolMessage, UserMessage
from mote.contracts.model import EndpointDescriptor
from mote.contracts.model.inference import FinalizedGenerateRequest
from mote.contracts.model.invocation import (
    CanonicalToolCall,
    GenerateOutput,
    ImageDescriptionOutput,
    ResolvedModelResponse,
    ResponseMode,
    WebSearchHitOutput,
    WebSearchOutput,
)
from mote.contracts.model.topology import DefaultRoute
from mote.contracts.ports.model.gateway import ModelRoute
from mote.runtime.models.model_calls import canonical_tool, describe_image, generate_finalized, web_search


class _Gateway:
    def __init__(self, output) -> None:
        self.output = output
        self.invocations = []
        self.request_transformers = []
        self.streams = []

    def supports_route(self, route_id) -> bool:
        return route_id == DefaultRoute()

    async def execute(self, invocation, *, request_transformer=None, stream=False, **_kwargs):
        self.invocations.append(invocation)
        self.request_transformers.append(request_transformer)
        self.streams.append(stream)
        return ResolvedModelResponse(
            output=self.output,
            endpoint_id="endpoint",
            endpoint_fingerprint="endpoint-fingerprint",
            model_or_deployment="model",
            tenant_fingerprint="tenant-fingerprint",
            credential_slot_id="slot",
            model_call_id=invocation.model_call_id,
        )

    async def resume(self, invocation, **kwargs):
        return await self.execute(invocation, **kwargs)


def _route(gateway, *, routing_decision_id=None):
    return ModelRoute(
        gateway=gateway,
        route_id=DefaultRoute(),
        routing_decision_id=routing_decision_id,
        profile=EndpointDescriptor(
            endpoint_id="endpoint",
            transport="openai",
            provider="openai",
            model="model",
            base_url_identity="https://example.test",
            credential_pool_id="test",
            lifecycle_revision="test",
        ),
    )


@pytest.mark.asyncio
async def test_text_call_projects_message_history_and_system_prompt() -> None:
    gateway = _Gateway(GenerateOutput(content="answer"))
    messages = [
        UserMessage(content="question"),
        AIMessage(
            content="",
            metadata={"tool_calls": [{"id": "call-1", "name": "Read", "args": {"path": "a"}}]},
        ),
        ToolMessage(
            content="contents",
            tool_call_id="call-1",
            tool_references=["Read"],
        ),
    ]

    result, _resolved = await generate_finalized(
        _route(gateway, routing_decision_id="decision-1"),
        FinalizedGenerateRequest(
            messages=tuple(messages),
            task="interactive",
            system_prompt="system",
        ),
        model_call_id="call",
    )

    assert result.content == "answer"
    invocation = gateway.invocations[0]
    assert invocation.route_id == DefaultRoute()
    assert invocation.routing_decision_id == "decision-1"
    assert invocation.requirements.response_mode is ResponseMode.TEXT
    assert invocation.input.system_prompt == "system"
    assert invocation.input.messages[1].tool_calls[0].name == "Read"
    assert invocation.input.messages[2].tool_call_id == "call-1"
    assert invocation.input.messages[2].tool_references == ("Read",)
    assert gateway.streams == [True]


@pytest.mark.asyncio
async def test_text_call_can_explicitly_disable_streaming() -> None:
    gateway = _Gateway(GenerateOutput(content="answer"))
    await generate_finalized(
        _route(gateway),
        FinalizedGenerateRequest(
            messages=(UserMessage(content="question"),),
            task="interactive",
            stream=False,
        ),
        model_call_id="call",
    )

    assert gateway.streams == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "read",
                "parameters": {"type": "object"},
            },
        },
        {
            "type": "function",
            "name": "Read",
            "description": "read",
            "parameters": {"type": "object"},
        },
        {
            "name": "Read",
            "description": "read",
            "input_schema": {"type": "object"},
            "defer_loading": True,
        },
    ],
)
async def test_native_call_canonicalizes_every_existing_tool_envelope(tool) -> None:
    gateway = _Gateway(
        GenerateOutput(
            content="",
            tool_calls=(CanonicalToolCall(id="call-1", name="Read", arguments={"path": "a"}),),
        )
    )
    result, _resolved = await generate_finalized(
        _route(gateway),
        FinalizedGenerateRequest(
            messages=(UserMessage(content="read"),),
            task="interactive",
            system_prompt="system",
            tools=(canonical_tool(tool),),
            response_mode=ResponseMode.NATIVE_TOOLS,
        ),
        model_call_id="call",
    )

    assert result.tool_calls[0].name == "Read"
    invocation = gateway.invocations[0]
    canonical = invocation.input.tools[0]
    assert canonical.name == "Read"
    assert canonical.description == "read"
    assert canonical.input_schema == {"type": "object"}
    assert canonical.defer_loading is bool(tool.get("defer_loading"))


@pytest.mark.asyncio
async def test_schema_call_sets_hard_planner_requirement() -> None:
    gateway = _Gateway(GenerateOutput(content='{"answer":"yes"}', structured={"answer": "yes"}))
    await generate_finalized(
        _route(gateway),
        FinalizedGenerateRequest(
            messages=(UserMessage(content="answer"),),
            task="interactive",
            output_schema={"type": "object"},
            response_mode=ResponseMode.NATIVE_SCHEMA,
        ),
        model_call_id="call",
    )

    invocation = gateway.invocations[0]
    assert invocation.requirements.response_mode is ResponseMode.NATIVE_SCHEMA
    assert invocation.requirements.needs_native_schema is True
    assert invocation.input.output_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_web_search_uses_canonical_task_operation() -> None:
    gateway = _Gateway(
        WebSearchOutput(
            hits=(
                WebSearchHitOutput(
                    title="Result",
                    url="https://example.test",
                    snippet="summary",
                ),
            )
        )
    )
    output = await web_search(
        _route(gateway),
        "query",
        model_call_id="call",
        allowed_domains=["example.test"],
        max_uses=3,
    )

    assert output.hits[0].title == "Result"
    invocation = gateway.invocations[0]
    assert invocation.operation.value == "web_search"
    assert invocation.input.allowed_domains == ("example.test",)
    assert invocation.requirements.needs_server_web_search is True


@pytest.mark.asyncio
async def test_image_description_uses_one_canonical_vision_generate() -> None:
    gateway = _Gateway(ImageDescriptionOutput(text="a tiny image"))
    content = b"image"
    artifact = ArtifactRef(
        artifact_id="image-1",
        revision=1,
        representation="png",
        kind="image",
        mime_type="image/png",
        content_ref="cas:image-1",
        digest=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )

    text = await describe_image(_route(gateway), artifact, model_call_id="call")

    assert text == "a tiny image"
    invocation = gateway.invocations[0]
    assert invocation.operation.value == "image_description"
    assert invocation.requirements.needs_vision is True
    assert invocation.input.artifact == artifact

"""Runtime model invocation adapter over gateway, transforms and artifact IO."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from mote.contracts.artifact import ArtifactRef
from mote.contracts.conversation import Message
from mote.contracts.conversation.fields import IMAGES, PDFS
from mote.contracts.events.envelope import JsonValue
from mote.contracts.model.inference import FinalizedGenerateRequest
from mote.contracts.model.invocation import (
    CanonicalMessage,
    CanonicalToolDefinition,
    GenerateInput,
    GenerateOutput,
    ImageDescriptionInput,
    ImageDescriptionOutput,
    ModelInvocation,
    RequestRequirements,
    ResolvedModelResponse,
    ResponseMode,
    TraceContext,
    WebSearchInput,
    WebSearchOutput,
)
from mote.contracts.model.operations import ModelOperation
from mote.contracts.ports.model.gateway import ModelRoute
from mote.kernel.telemetry.events import current_span_id
from mote.runtime.models.message_wire import canonical_message_from_model_wire, message_to_model_wire


async def _execute_generate(
    route: ModelRoute,
    messages: tuple[Message, ...],
    *,
    model_call_id: str,
    task: str,
    system_prompt: str = "",
    tools: tuple[CanonicalToolDefinition, ...] = (),
    output_schema: Mapping[str, JsonValue] | None = None,
    response_mode: ResponseMode = ResponseMode.TEXT,
    stream: bool = True,
    resume: bool = False,
    trace_id: str = "",
) -> tuple[GenerateOutput, ResolvedModelResponse]:
    canonical_messages, needs_vision, needs_pdf = _canonical_messages_from(messages)
    canonical_tools = tools
    invocation = ModelInvocation(
        model_call_id=model_call_id,
        routing_decision_id=route.routing_decision_id,
        route_id=route.route_id,
        task=task,
        operation=ModelOperation.GENERATE,
        input=GenerateInput(
            messages=canonical_messages,
            system_prompt=system_prompt,
            tools=canonical_tools,
            output_schema=output_schema,
        ),
        requirements=RequestRequirements(
            response_mode=response_mode,
            needs_tools=bool(canonical_tools),
            needs_native_schema=response_mode is ResponseMode.NATIVE_SCHEMA,
            needs_vision=needs_vision,
            needs_pdf=needs_pdf,
            needs_native_tool_search=any(tool.defer_loading for tool in canonical_tools),
        ),
        trace=TraceContext(trace_id=trace_id, parent_span_id=current_span_id()),
    )
    method = route.gateway.resume if resume else route.gateway.execute
    response = await method(
        invocation,
        request_transformer=route.request_transformer,
        stream=stream,
        session_fact_sink=route.session_fact_sink,
        artifact_resolver=route.artifact_resolver,
    )
    output = response.output
    if not isinstance(output, GenerateOutput):
        raise TypeError("generate route returned a non-generate output")
    return output, response


async def generate_finalized(
    route: ModelRoute,
    request: FinalizedGenerateRequest,
    *,
    model_call_id: str,
) -> tuple[GenerateOutput, ResolvedModelResponse]:
    """Execute the one typed Kernel→Runtime generate operation."""

    return await _execute_generate(
        route,
        request.messages,
        model_call_id=model_call_id,
        task=request.task,
        system_prompt=request.system_prompt,
        tools=request.tools,
        output_schema=request.output_schema,
        response_mode=request.response_mode,
        stream=request.stream,
        resume=request.resume,
        trace_id=request.trace_id,
    )


async def web_search(
    route: ModelRoute,
    query: str,
    *,
    model_call_id: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_uses: int = 8,
    trace_id: str = "",
) -> WebSearchOutput:
    invocation = ModelInvocation(
        model_call_id=model_call_id,
        routing_decision_id=route.routing_decision_id,
        route_id=route.route_id,
        task="web_search",
        operation=ModelOperation.WEB_SEARCH,
        input=WebSearchInput(
            query=query,
            allowed_domains=tuple(allowed_domains or ()),
            blocked_domains=tuple(blocked_domains or ()),
            max_uses=max_uses,
        ),
        requirements=RequestRequirements(needs_server_web_search=True),
        trace=TraceContext(trace_id=trace_id, parent_span_id=current_span_id()),
    )
    response = await route.gateway.execute(
        invocation,
        request_transformer=route.request_transformer,
        session_fact_sink=route.session_fact_sink,
        artifact_resolver=route.artifact_resolver,
    )
    if not isinstance(response.output, WebSearchOutput):
        raise TypeError("web-search route returned a non-search output")
    return response.output


async def describe_image(
    route: ModelRoute,
    artifact: ArtifactRef,
    *,
    model_call_id: str,
    prompt: str = "",
    trace_id: str = "",
) -> str:
    invocation = ModelInvocation(
        model_call_id=model_call_id,
        routing_decision_id=route.routing_decision_id,
        route_id=route.route_id,
        task="image_description",
        operation=ModelOperation.IMAGE_DESCRIPTION,
        input=ImageDescriptionInput(artifact=artifact, prompt=prompt),
        requirements=RequestRequirements(needs_vision=True),
        trace=TraceContext(trace_id=trace_id, parent_span_id=current_span_id()),
    )
    response = await route.gateway.execute(
        invocation,
        request_transformer=route.request_transformer,
        session_fact_sink=route.session_fact_sink,
        artifact_resolver=route.artifact_resolver,
    )
    if not isinstance(response.output, ImageDescriptionOutput):
        raise TypeError("image-description route returned a non-description output")
    return response.output.text


def _canonical_messages_from(
    items: tuple[Message, ...],
) -> tuple[tuple[CanonicalMessage, ...], bool, bool]:
    messages: list[CanonicalMessage] = []
    needs_vision = False
    needs_pdf = False
    for item in items:
        if not isinstance(item, Message):
            raise TypeError("finalized model messages must be canonical Message values")
        metadata = item.metadata
        wire = message_to_model_wire(item)
        images = metadata.get(IMAGES)
        pdfs = metadata.get(PDFS)
        if images or pdfs:
            wire = _with_media(wire, images, pdfs)
            needs_vision = needs_vision or bool(images)
            needs_pdf = needs_pdf or bool(pdfs)
        messages.append(canonical_message_from_model_wire(wire))
    return tuple(messages), needs_vision, needs_pdf


def canonical_tool(tool: dict) -> CanonicalToolDefinition:
    nested = tool.get("function")
    if isinstance(nested, dict):
        name = nested.get("name", "")
        description = nested.get("description", "") or ""
        schema = nested.get("parameters") or {}
    else:
        name = tool.get("name", "")
        description = tool.get("description", "") or ""
        schema = tool.get("input_schema") or tool.get("parameters") or {}
    return CanonicalToolDefinition(
        name=name,
        description=description,
        input_schema=schema,
        defer_loading=bool(tool.get("defer_loading")),
    )


def _with_media(wire: dict[str, Any], images: Any, pdfs: Any) -> dict[str, Any]:
    content = wire.get("content")
    blocks: list[dict[str, Any]] = (
        list(content) if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    )
    image_items = [images] if isinstance(images, str) else list(images or ())
    for image in image_items:
        url = image if image.startswith(("http", "data:")) else _image_data_url(image)
        if url is None:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    pdf_items = [pdfs] if isinstance(pdfs, str) else list(pdfs or ())
    for pdf in pdf_items:
        blocks.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf,
                },
                "citations": {"enabled": True},
            }
        )
    return {**wire, "content": blocks}


def _image_data_url(payload: str) -> str | None:
    media_type = "image/jpeg"
    try:
        header = base64.b64decode(payload[:64], validate=False)
    except (binascii.Error, ValueError):
        header = b""
    stripped = header.lstrip()
    if stripped.startswith((b"<svg", b"<?xml")):
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        media_type = "image/webp"
    return f"data:{media_type};base64,{payload}"


__all__ = [
    "canonical_tool",
    "describe_image",
    "generate_finalized",
    "web_search",
]

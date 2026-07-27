"""Provider-neutral model invocation builders used by Kernel call sites."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from mote.contracts.artifacts import ArtifactRef
from mote.contracts.constants.messages import CACHE_INTENT, IMAGES, PDFS
from mote.contracts.models.invocation import (
    CanonicalMessage,
    CanonicalToolCall,
    CanonicalToolDefinition,
    GenerateInput,
    GenerateOutput,
    ImageDescriptionInput,
    ImageDescriptionOutput,
    ModelInvocation,
    ModelOperation,
    RequestRequirements,
    ResolvedModelResponse,
    ResponseMode,
    TraceContext,
    WebSearchInput,
    WebSearchOutput,
)
from mote.contracts.ports import ModelRoute
from mote.contracts.schema import Message
from mote.kernel.telemetry import current_span_id


async def generate(
    route: ModelRoute,
    messages: Any,
    *,
    model_call_id: str,
    task: str,
    system_prompt: str = "",
    tools: list[dict] | None = None,
    output_schema: dict | None = None,
    response_mode: ResponseMode = ResponseMode.TEXT,
    stream: bool = True,
    resume: bool = False,
    trace_id: str = "",
) -> tuple[GenerateOutput, ResolvedModelResponse]:
    canonical_messages, needs_vision, needs_pdf = canonical_messages_from(messages)
    canonical_tools = tuple(canonical_tool(tool) for tool in tools or ())
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


def canonical_messages_from(
    value: Any,
) -> tuple[tuple[CanonicalMessage, ...], bool, bool]:
    items = value if isinstance(value, list) else [value]
    messages: list[CanonicalMessage] = []
    needs_vision = False
    needs_pdf = False
    for item in items:
        metadata = item.metadata if isinstance(item, Message) else {}
        wire = item.to_dict() if isinstance(item, Message) else item
        if isinstance(wire, str):
            wire = {"role": "user", "content": wire}
        elif not isinstance(wire, dict):
            wire = {"role": "user", "content": str(wire)}
        images = metadata.get(IMAGES)
        pdfs = metadata.get(PDFS)
        if images or pdfs:
            wire = _with_media(wire, images, pdfs)
            needs_vision = needs_vision or bool(images)
            needs_pdf = needs_pdf or bool(pdfs)
        messages.append(_canonical_message(wire))
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


def _canonical_message(wire: dict[str, Any]) -> CanonicalMessage:
    calls: list[CanonicalToolCall] = []
    for call in wire.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(
            CanonicalToolCall(
                id=call.get("id", ""),
                name=function.get("name", ""),
                arguments=arguments or {},
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
    "canonical_messages_from",
    "canonical_tool",
    "describe_image",
    "generate",
    "web_search",
]

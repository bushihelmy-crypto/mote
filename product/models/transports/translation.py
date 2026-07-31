"""Generation-pinned provider response translation into canonical model output."""

from __future__ import annotations

import json
from typing import Any

from mote.contracts.model.invocation import CanonicalModelResponse, CanonicalToolCall, GenerateOutput, ModelUsage


def translate_openai_chat(payload: dict[str, Any]) -> CanonicalModelResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("OpenAI response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("OpenAI choice has no message")
    message = choice["message"]
    content = message.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        raise ValueError("OpenAI message content has unsupported shape")
    tool_calls: list[CanonicalToolCall] = []
    for raw_call in message.get("tool_calls") or ():
        if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
            raise ValueError("OpenAI tool call has invalid shape")
        function = raw_call["function"]
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("OpenAI tool arguments are malformed") from exc
        if not isinstance(arguments, dict):
            raise ValueError("OpenAI tool arguments must be an object")
        tool_calls.append(
            CanonicalToolCall(
                id=str(raw_call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    return CanonicalModelResponse(
        output=GenerateOutput(content=text, tool_calls=tuple(tool_calls)),
        usage=_openai_usage(payload.get("usage")),
        provider_request_id=str(payload["id"]) if payload.get("id") else None,
    )


def translate_openai_chat_stream(
    chunks: tuple[dict[str, Any], ...],
) -> CanonicalModelResponse:
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    request_id: str | None = None
    for chunk in chunks:
        if chunk.get("id"):
            request_id = str(chunk["id"])
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise ValueError("OpenAI stream chunk has no choices")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("OpenAI stream choice has invalid shape")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("OpenAI stream delta has invalid shape")
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for raw_call in delta.get("tool_calls") or ():
                if not isinstance(raw_call, dict) or not isinstance(raw_call.get("index"), int):
                    raise ValueError("OpenAI stream tool call lacks index")
                record = calls.setdefault(raw_call["index"], {"id": "", "name": "", "arguments": ""})
                if raw_call.get("id"):
                    record["id"] = str(raw_call["id"])
                function = raw_call.get("function") or {}
                if not isinstance(function, dict):
                    raise ValueError("OpenAI stream tool function is invalid")
                if function.get("name"):
                    record["name"] += str(function["name"])
                if function.get("arguments"):
                    record["arguments"] += str(function["arguments"])
    tool_calls = []
    for index in sorted(calls):
        record = calls[index]
        try:
            arguments = json.loads(record["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("OpenAI streamed tool arguments are malformed") from exc
        if not isinstance(arguments, dict):
            raise ValueError("OpenAI streamed tool arguments must be an object")
        tool_calls.append(CanonicalToolCall(id=record["id"], name=record["name"], arguments=arguments))
    return CanonicalModelResponse(
        output=GenerateOutput(content="".join(content), tool_calls=tuple(tool_calls)),
        usage=_openai_usage(usage),
        provider_request_id=request_id,
    )


def translate_openai_responses(payload: dict[str, Any]) -> CanonicalModelResponse:
    output = payload.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses payload has no output array")
    text: list[str] = []
    tool_calls: list[CanonicalToolCall] = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Responses output item has invalid shape")
        item_type = item.get("type")
        if item_type == "message":
            content = item.get("content") or ()
            if not isinstance(content, list):
                raise ValueError("Responses message content has invalid shape")
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("Responses content block has invalid shape")
                if block.get("type") in {"output_text", "text"}:
                    value = block.get("text")
                    if not isinstance(value, str):
                        raise ValueError("Responses text block has no text")
                    text.append(value)
        elif item_type == "function_call":
            arguments = item.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ValueError("Responses function arguments are malformed") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Responses function arguments must be an object")
            tool_calls.append(
                CanonicalToolCall(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=arguments,
                )
            )
    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return CanonicalModelResponse(
        output=GenerateOutput(content="".join(text), tool_calls=tuple(tool_calls)),
        usage=ModelUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cache_read_tokens=int(input_details.get("cached_tokens") or 0),
            reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
        ),
        provider_request_id=str(payload["id"]) if payload.get("id") else None,
    )


def translate_anthropic_message(payload: dict[str, Any]) -> CanonicalModelResponse:
    content = payload.get("content")
    if not isinstance(content, list):
        raise ValueError("Anthropic message has no content array")
    text: list[str] = []
    tool_calls: list[CanonicalToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Anthropic content block has invalid shape")
        if block.get("type") == "text":
            value = block.get("text")
            if not isinstance(value, str):
                raise ValueError("Anthropic text block has no text")
            text.append(value)
        elif block.get("type") == "tool_use":
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                raise ValueError("Anthropic tool input must be an object")
            tool_calls.append(
                CanonicalToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                )
            )
    usage = payload.get("usage")
    normalized_usage = usage if isinstance(usage, dict) else {}
    input_tokens = int(normalized_usage.get("input_tokens") or 0)
    output_tokens = int(normalized_usage.get("output_tokens") or 0)
    return CanonicalModelResponse(
        output=GenerateOutput(content="".join(text), tool_calls=tuple(tool_calls)),
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=int(normalized_usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(normalized_usage.get("cache_creation_input_tokens") or 0),
        ),
        provider_request_id=str(payload["id"]) if payload.get("id") else None,
    )


def translate_anthropic_stream(events: tuple[dict[str, Any], ...]) -> CanonicalModelResponse:
    message: dict[str, Any] = {"content": [], "usage": {}}
    blocks: dict[int, dict[str, Any]] = {}
    tool_json: dict[int, list[str]] = {}
    for event in events:
        event_type = event.get("type")
        if event_type == "message_start":
            started = event.get("message")
            if not isinstance(started, dict):
                raise ValueError("Anthropic message_start has no message")
            if started.get("id"):
                message["id"] = started["id"]
            if isinstance(started.get("usage"), dict):
                message["usage"] = dict(started["usage"])
        elif event_type == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                raise ValueError("Anthropic block start is malformed")
            blocks[index] = dict(block)
            if block.get("type") == "tool_use":
                tool_json[index] = []
        elif event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if not isinstance(index, int) or not isinstance(delta, dict):
                raise ValueError("Anthropic block delta is malformed")
            block = blocks.get(index)
            if block is None:
                raise ValueError("Anthropic block delta precedes block start")
            if delta.get("type") == "text_delta":
                block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                tool_json.setdefault(index, []).append(str(delta.get("partial_json") or ""))
        elif event_type == "message_delta" and isinstance(event.get("usage"), dict):
            message["usage"].update(event["usage"])
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "tool_use":
            encoded = "".join(tool_json.get(index, ()))
            try:
                arguments = json.loads(encoded or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Anthropic streamed tool input is malformed") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Anthropic streamed tool input must be an object")
            block["input"] = arguments
        message["content"].append(block)
    return translate_anthropic_message(message)


def translate_google_generate_content(payloads: tuple[dict[str, Any], ...]) -> CanonicalModelResponse:
    text: list[str] = []
    tool_calls: list[CanonicalToolCall] = []
    usage: dict[str, Any] = {}
    for payload in payloads:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError("Google response must contain exactly one candidate")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ValueError("Google candidate has invalid shape")
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            raise ValueError("Google candidate content has invalid shape")
        parts = content.get("parts") or ()
        if not isinstance(parts, list):
            raise ValueError("Google candidate parts have invalid shape")
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("Google content part has invalid shape")
            if isinstance(part.get("text"), str):
                text.append(part["text"])
            function = part.get("functionCall")
            if function is not None:
                if not isinstance(function, dict) or not isinstance(function.get("args"), dict):
                    raise ValueError("Google function call has invalid shape")
                tool_calls.append(
                    CanonicalToolCall(
                        id=str(function.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=function["args"],
                    )
                )
        if isinstance(payload.get("usageMetadata"), dict):
            usage = payload["usageMetadata"]
    input_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    total_tokens = int(usage.get("totalTokenCount") or input_tokens + output_tokens)
    return CanonicalModelResponse(
        output=GenerateOutput(content="".join(text), tool_calls=tuple(tool_calls)),
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=int(usage.get("cachedContentTokenCount") or 0),
            reasoning_tokens=int(usage.get("thoughtsTokenCount") or 0),
        ),
    )


def _openai_usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    prompt_details = value.get("prompt_tokens_details") or {}
    completion_details = value.get("completion_tokens_details") or {}
    return ModelUsage(
        input_tokens=int(value.get("prompt_tokens") or 0),
        output_tokens=int(value.get("completion_tokens") or 0),
        total_tokens=int(value.get("total_tokens") or 0),
        cache_read_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
    )

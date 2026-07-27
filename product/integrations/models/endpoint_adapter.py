"""Bound Product adapter from canonical model calls to provider wire envelopes."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from mote.contracts.artifacts import ResolvedArtifact
from mote.contracts.config.llm import LLMType
from mote.contracts.models.failover import EndpointDescriptor, FailureDisposition
from mote.contracts.models.invocation import (
    CanonicalMessage,
    CanonicalModelResponse,
    CanonicalToolCall,
    CanonicalToolDefinition,
    GenerateInput,
    GenerateOutput,
    ImageDescriptionInput,
    ImageDescriptionOutput,
    ModelInvocation,
    ModelQuotaObservation,
    ModelUsage,
    ResponseMode,
    WebSearchHitOutput,
    WebSearchOutput,
)
from mote.contracts.models.profile import profile_for
from mote.runtime.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMEmptyResponseError,
    LLMResponseParseError,
    classify_llm_error,
)
from mote.runtime.models.clients.base import BaseLLM
from mote.runtime.models.cost import CostTracker, PricingMode, TokenUsage
from mote.runtime.models.failover.policy import classify_failure
from mote.runtime.models.ratelimit import RateLimitSnapshot, RateLimitTracker

_OPENAI_RESPONSES = LLMType.OPENAI_RESPONSES.value
_ANTHROPIC = LLMType.ANTHROPIC.value


class ProductModelEndpointAdapter:
    """One endpoint and one credential slot, with no retry or route state."""

    def __init__(
        self,
        *,
        endpoint_id: str,
        credential_slot_id: str,
        tenant_fingerprint: str,
        transport: str,
        llm: BaseLLM,
        prepare_credential: Callable[[], bool] | None = None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.credential_slot_id = credential_slot_id
        self.tenant_fingerprint = tenant_fingerprint
        self._transport = transport
        self._llm = llm
        self._prepare_credential = prepare_credential
        self._credential_prepared = prepare_credential is None

    async def execute_once(
        self,
        invocation: ModelInvocation,
        endpoint: EndpointDescriptor,
        *,
        timeout_seconds: float,
        stream: bool = False,
        artifact: ResolvedArtifact | None = None,
    ) -> CanonicalModelResponse:
        if endpoint.endpoint_id != self.endpoint_id:
            raise LLMBadRequestError("adapter endpoint binding does not match request")
        if not self._credential_prepared:
            self._credential_prepared = True
            if self._prepare_credential is None or not self._prepare_credential():
                raise LLMAuthenticationError("selected OAuth refresh slot is unavailable")

        tracker = CostTracker(mode=self._pricing_mode())
        quota_tracker = RateLimitTracker()
        self._llm.cost_manager = tracker
        self._llm.rate_limit_tracker = quota_tracker
        if isinstance(invocation.input, GenerateInput):
            response = await self._generate_once(
                invocation,
                invocation.input,
                endpoint,
                timeout_seconds=timeout_seconds,
                stream=stream,
            )
            raw_usage = getattr(response, "usage", None)
            output = self._generate_output(invocation, response)
            request_id = getattr(response, "id", None)
        elif isinstance(invocation.input, ImageDescriptionInput):
            if artifact is None or artifact.ref != invocation.input.artifact:
                raise LLMBadRequestError("image-description artifact was not resolved for this invocation")
            response = await self._describe_image_once(
                invocation,
                invocation.input,
                artifact,
                endpoint,
                timeout_seconds=timeout_seconds,
                stream=stream,
            )
            raw_usage = getattr(response, "usage", None)
            text = self._llm.get_choice_text(response) or ""
            if not text.strip():
                raise LLMEmptyResponseError("The image description is empty.")
            output = ImageDescriptionOutput(text=text)
            request_id = getattr(response, "id", None)
        elif invocation.input.kind == "web_search":
            hits = await self._llm.aweb_search(
                invocation.input.query,
                allowed_domains=list(invocation.input.allowed_domains) or None,
                blocked_domains=list(invocation.input.blocked_domains) or None,
                max_uses=invocation.input.max_uses,
            )
            raw_usage = None
            output = WebSearchOutput(
                hits=tuple(
                    WebSearchHitOutput(
                        title=hit.title,
                        url=hit.url,
                        snippet=hit.snippet,
                    )
                    for hit in hits
                )
            )
            request_id = None
        else:
            raise LLMBadRequestError(
                f"operation {invocation.operation.value!r} requires an artifact "
                "projection that is not bound to this endpoint adapter"
            )

        usage = self._normalized_usage(raw_usage, tracker)
        quota = self._quota_observation(quota_tracker, endpoint)
        return CanonicalModelResponse(
            output=output,
            usage=_contract_usage(usage),
            cost_usd=Decimal(str(tracker.last_cost)),
            provider_request_id=request_id if isinstance(request_id, str) else None,
            quota=quota,
        )

    def classify(self, exc: Exception) -> FailureDisposition:
        translated = classify_llm_error(exc) or exc
        return classify_failure(translated)

    async def aclose(self) -> None:
        await self._llm.aclose()

    async def _generate_once(
        self,
        invocation: ModelInvocation,
        request: GenerateInput,
        endpoint: EndpointDescriptor,
        *,
        timeout_seconds: float,
        stream: bool,
    ) -> Any:
        messages = self._project_messages(request)
        kwargs: dict[str, Any] = {"raise_if_empty": False}
        mode = invocation.requirements.response_mode
        if request.tools:
            kwargs["tools"] = [self._project_tool(tool, endpoint) for tool in request.tools]
            kwargs["tool_choice"] = "auto"
        if mode is ResponseMode.NATIVE_SCHEMA:
            if request.output_schema is None:
                raise LLMBadRequestError("native schema response mode requires an output schema")
            native_schema = self._llm.native_schema_request(request.output_schema)
            if native_schema is None:
                raise LLMBadRequestError(f"transport {self._transport!r} does not support native schema output")
            kwargs.update(native_schema)

        timeout = max(1, math.ceil(timeout_seconds))
        if stream:
            return await self._llm._achat_completion_stream_tool(
                messages,
                timeout=timeout,
                **kwargs,
            )
        return await self._llm._achat_completion(messages, timeout=timeout, **kwargs)

    async def _describe_image_once(
        self,
        invocation: ModelInvocation,
        request: ImageDescriptionInput,
        artifact: ResolvedArtifact,
        endpoint: EndpointDescriptor,
        *,
        timeout_seconds: float,
        stream: bool,
    ) -> Any:
        encoded = base64.b64encode(artifact.content).decode("ascii")
        media_url = f"data:{artifact.ref.mime_type};base64,{encoded}"
        projected = GenerateInput(
            messages=(
                CanonicalMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": request.prompt},
                        {"type": "image_url", "image_url": {"url": media_url}},
                    ],
                ),
            )
        )
        return await self._generate_once(
            invocation,
            projected,
            endpoint,
            timeout_seconds=timeout_seconds,
            stream=stream,
        )

    def _generate_output(
        self,
        invocation: ModelInvocation,
        response: Any,
    ) -> GenerateOutput:
        content = self._llm.get_choice_text(response) or ""
        calls = self._llm.get_choice_tool_calls(response)
        if not content.strip() and not calls:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        structured: Any = None
        if invocation.requirements.response_mode in {
            ResponseMode.NATIVE_SCHEMA,
            ResponseMode.PROMPTED_SCHEMA,
        }:
            try:
                structured = json.loads(content)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LLMResponseParseError(
                    "model response is not valid JSON for structured output",
                    cause=exc,
                ) from exc
        return GenerateOutput(
            content=content,
            tool_calls=tuple(
                CanonicalToolCall(
                    id=call.get("id", ""),
                    name=call["name"],
                    arguments=call.get("arguments") or {},
                )
                for call in calls
            ),
            structured=structured,
        )

    def _project_messages(self, request: GenerateInput) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.extend(_project_message(message) for message in request.messages)
        return messages

    def _project_tool(
        self,
        tool: CanonicalToolDefinition,
        endpoint: EndpointDescriptor,
    ) -> dict[str, Any]:
        schema = dict(tool.input_schema) or {"type": "object", "properties": {}}
        transformer = profile_for(endpoint.model).json_schema_transformer
        if transformer is not None:
            schema = transformer(schema)
        if self._transport == _ANTHROPIC:
            projected = {
                "name": tool.name,
                "description": tool.description,
                "input_schema": schema,
            }
        elif self._transport == _OPENAI_RESPONSES:
            projected = {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": schema,
            }
        else:
            projected = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            }
        if tool.defer_loading and endpoint.capabilities.supports_native_tool_search:
            projected["defer_loading"] = True
        return projected

    def _normalized_usage(
        self,
        raw_usage: Any,
        tracker: CostTracker,
    ) -> TokenUsage:
        if not tracker.last_usage.is_zero():
            return tracker.last_usage
        if self._transport == _ANTHROPIC:
            return TokenUsage.from_anthropic(raw_usage)
        return TokenUsage.from_usage(raw_usage)

    def _pricing_mode(self) -> PricingMode:
        if self._transport == LLMType.FIREWORKS.value:
            return PricingMode.FIREWORKS
        if self._transport == LLMType.OPEN_LLM.value:
            return PricingMode.FREE
        return PricingMode.STANDARD

    def _quota_observation(
        self,
        tracker: RateLimitTracker,
        endpoint: EndpointDescriptor,
    ) -> ModelQuotaObservation | None:
        provider = getattr(self._llm, "provider_label", None) or self._transport
        model = getattr(self._llm, "model", None) or endpoint.model
        snapshot = tracker.get(provider, model)
        if snapshot is None:
            return None
        return _contract_quota(snapshot)


def _project_message(message: CanonicalMessage) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.name is not None:
        projected["name"] = message.name
    if message.tool_call_id is not None:
        projected["tool_call_id"] = message.tool_call_id
    if message.tool_references:
        projected["_tool_references"] = list(message.tool_references)
    if message.cache_intent is not None:
        projected["_cache_intent"] = message.cache_intent
    if message.tool_calls:
        projected["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in message.tool_calls
        ]
    return projected


def _contract_usage(usage: TokenUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cache_read_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_creation_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )


def _contract_quota(snapshot: RateLimitSnapshot) -> ModelQuotaObservation:
    return ModelQuotaObservation(
        limit_requests=snapshot.limit_requests,
        remaining_requests=snapshot.remaining_requests,
        reset_requests_after_seconds=(snapshot.normalized_reset_requests_seconds),
        limit_tokens=snapshot.limit_tokens,
        remaining_tokens=snapshot.remaining_tokens,
        reset_tokens_after_seconds=snapshot.normalized_reset_tokens_seconds,
        retry_after_seconds=snapshot.retry_after_seconds,
    )


__all__ = ["ProductModelEndpointAdapter"]

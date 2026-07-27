"""Provider-neutral model invocation and response contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from mote.contracts.artifacts import ArtifactRef


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelOperation(StrEnum):
    GENERATE = "generate"
    WEB_SEARCH = "web_search"
    IMAGE_DESCRIPTION = "image_description"


class ResponseMode(StrEnum):
    TEXT = "text"
    NATIVE_TOOLS = "native_tools"
    NATIVE_SCHEMA = "native_schema"
    PROMPTED_SCHEMA = "prompted_schema"


class CanonicalToolCall(_FrozenContract):
    id: str = ""
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class CanonicalMessage(_FrozenContract):
    role: str = Field(min_length=1)
    content: JsonValue
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    tool_references: tuple[str, ...] = ()
    cache_intent: str | None = None


class CanonicalToolDefinition(_FrozenContract):
    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    defer_loading: bool = False


class GenerateInput(_FrozenContract):
    kind: Literal["generate"] = "generate"
    messages: tuple[CanonicalMessage, ...]
    system_prompt: str = ""
    tools: tuple[CanonicalToolDefinition, ...] = ()
    output_schema: dict[str, JsonValue] | None = None


class WebSearchInput(_FrozenContract):
    kind: Literal["web_search"] = "web_search"
    query: str = Field(min_length=1)
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int = Field(default=8, ge=1)


class ImageDescriptionInput(_FrozenContract):
    kind: Literal["image_description"] = "image_description"
    artifact: ArtifactRef
    prompt: str = ""


CanonicalModelInput = Annotated[
    Union[GenerateInput, WebSearchInput, ImageDescriptionInput],
    Field(discriminator="kind"),
]


class RequestRequirements(_FrozenContract):
    response_mode: ResponseMode = ResponseMode.TEXT
    needs_tools: bool = False
    needs_native_schema: bool = False
    needs_server_web_search: bool = False
    needs_vision: bool = False
    needs_pdf: bool = False
    needs_native_tool_search: bool = False
    min_context_tokens: int = Field(default=0, ge=0)
    governance_domain: str = "default"
    allowed_regions: frozenset[str] = frozenset()
    data_classification: str = "default"


class TraceContext(_FrozenContract):
    trace_id: str = ""
    parent_span_id: str | None = None


class ModelInvocation(_FrozenContract):
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    routing_decision_id: str | None = None
    route_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    operation: ModelOperation
    input: CanonicalModelInput
    requirements: RequestRequirements = Field(default_factory=RequestRequirements)
    budget_profile: str = "default"
    trace: TraceContext = Field(default_factory=TraceContext)

    @model_validator(mode="after")
    def _operation_matches_input(self) -> "ModelInvocation":
        if self.operation.value != self.input.kind:
            raise ValueError(f"operation {self.operation.value!r} does not match input kind " f"{self.input.kind!r}")
        return self


class GenerateOutput(_FrozenContract):
    kind: Literal["generate"] = "generate"
    content: str = ""
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    structured: JsonValue = None


class WebSearchHitOutput(_FrozenContract):
    title: str
    url: str
    snippet: str = ""


class WebSearchOutput(_FrozenContract):
    kind: Literal["web_search"] = "web_search"
    hits: tuple[WebSearchHitOutput, ...] = ()


class ImageDescriptionOutput(_FrozenContract):
    kind: Literal["image_description"] = "image_description"
    text: str


CanonicalModelOutput = Annotated[
    Union[GenerateOutput, WebSearchOutput, ImageDescriptionOutput],
    Field(discriminator="kind"),
]


class ModelUsage(_FrozenContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class ModelQuotaObservation(_FrozenContract):
    """Provider-neutral quota facts observed on one wire response."""

    limit_requests: int | None = Field(default=None, ge=0)
    remaining_requests: int | None = Field(default=None, ge=0)
    reset_requests_after_seconds: float | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    reset_tokens_after_seconds: float | None = Field(default=None, ge=0)
    retry_after_seconds: float | None = Field(default=None, gt=0)


class CanonicalModelResponse(_FrozenContract):
    schema_version: Literal[1] = 1
    output: CanonicalModelOutput
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: Decimal = Decimal("0")
    provider_request_id: str | None = None
    quota: ModelQuotaObservation | None = None


class ResolvedModelResponse(_FrozenContract):
    schema_version: Literal[1] = 1
    output: CanonicalModelOutput
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: Decimal = Decimal("0")
    endpoint_id: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    model_or_deployment: str = Field(min_length=1)
    tenant_fingerprint: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    provider: str = "unknown"
    transport: str = "unknown"
    model_call_id: str = ""
    successful_attempt_id: str = ""
    summary: Any = None


__all__ = [
    "CanonicalMessage",
    "CanonicalModelInput",
    "CanonicalModelOutput",
    "CanonicalModelResponse",
    "CanonicalToolCall",
    "CanonicalToolDefinition",
    "GenerateInput",
    "GenerateOutput",
    "ImageDescriptionInput",
    "ImageDescriptionOutput",
    "ModelInvocation",
    "ModelOperation",
    "ModelQuotaObservation",
    "ModelUsage",
    "RequestRequirements",
    "ResolvedModelResponse",
    "ResponseMode",
    "TraceContext",
    "WebSearchHitOutput",
    "WebSearchInput",
    "WebSearchOutput",
]

"""Provider-neutral model invocation and response contracts."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mote.contracts.artifact import ArtifactRef
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import RouteId


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResponseMode(StrEnum):
    TEXT = "text"
    NATIVE_TOOLS = "native_tools"
    NATIVE_SCHEMA = "native_schema"
    PROMPTED_SCHEMA = "prompted_schema"


class CanonicalToolCall(_FrozenContract):
    id: str = ""
    name: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _freeze_arguments(cls, value: object) -> Mapping[str, JsonValue]:
        frozen = freeze_json(value, path="arguments")
        if not isinstance(frozen, Mapping):
            raise ValueError("tool-call arguments must be a JSON object")
        return frozen


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
    input_schema: Mapping[str, JsonValue] = Field(default_factory=dict)
    defer_loading: bool = False

    @field_validator("input_schema")
    @classmethod
    def _freeze_input_schema(cls, value: object) -> Mapping[str, JsonValue]:
        frozen = freeze_json(value, path="tool input schema")
        if not isinstance(frozen, Mapping):
            raise ValueError("tool input schema must be a JSON object")
        return frozen


class GenerateInput(_FrozenContract):
    kind: Literal["generate"] = "generate"
    messages: tuple[CanonicalMessage, ...]
    system_prompt: str = ""
    tools: tuple[CanonicalToolDefinition, ...] = ()
    output_schema: Mapping[str, JsonValue] | None = None

    @field_validator("output_schema")
    @classmethod
    def _freeze_output_schema(cls, value: object | None) -> Mapping[str, JsonValue] | None:
        if value is None:
            return None
        frozen = freeze_json(value, path="model output schema")
        if not isinstance(frozen, Mapping):
            raise ValueError("model output schema must be a JSON object")
        return frozen


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


class EmbeddingInput(_FrozenContract):
    kind: Literal["embedding"] = "embedding"
    values: tuple[str, ...] = Field(min_length=1)
    dimensions: int | None = Field(default=None, ge=1)


class ImageGenerationInput(_FrozenContract):
    kind: Literal["image_generation"] = "image_generation"
    prompt: str = Field(min_length=1)
    options: Mapping[str, JsonValue] = Field(default_factory=dict)


class SpeechInput(_FrozenContract):
    kind: Literal["speech"] = "speech"
    text: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    options: Mapping[str, JsonValue] = Field(default_factory=dict)


class TranscriptionInput(_FrozenContract):
    kind: Literal["transcription"] = "transcription"
    artifact: ArtifactRef
    options: Mapping[str, JsonValue] = Field(default_factory=dict)


CanonicalModelInput = Annotated[
    Union[
        GenerateInput,
        EmbeddingInput,
        ImageGenerationInput,
        SpeechInput,
        TranscriptionInput,
        WebSearchInput,
        ImageDescriptionInput,
    ],
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
    route_id: RouteId
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
    content_artifact: ArtifactRef | None = None
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    structured: JsonValue = None

    @model_validator(mode="after")
    def _content_location(self) -> "GenerateOutput":
        if self.content_artifact is not None and self.content:
            raise ValueError("generate content must be inline or ArtifactRef, not both")
        return self


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


class EmbeddingOutput(_FrozenContract):
    kind: Literal["embedding"] = "embedding"
    vectors: tuple[tuple[float, ...], ...]


class ImageGenerationOutput(_FrozenContract):
    kind: Literal["image_generation"] = "image_generation"
    artifacts: tuple[ArtifactRef, ...] = ()
    provider_items: tuple[Mapping[str, JsonValue], ...] = ()


class SpeechOutput(_FrozenContract):
    kind: Literal["speech"] = "speech"
    artifact: ArtifactRef


class TranscriptionOutput(_FrozenContract):
    kind: Literal["transcription"] = "transcription"
    text: str


CanonicalModelOutput = Annotated[
    Union[
        GenerateOutput,
        EmbeddingOutput,
        ImageGenerationOutput,
        SpeechOutput,
        TranscriptionOutput,
        WebSearchOutput,
        ImageDescriptionOutput,
    ],
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
    model_call_id: str = Field(min_length=1)
    successful_attempt_id: str | None = None


__all__ = [
    "CanonicalMessage",
    "CanonicalModelInput",
    "CanonicalModelOutput",
    "CanonicalModelResponse",
    "CanonicalToolCall",
    "CanonicalToolDefinition",
    "GenerateInput",
    "GenerateOutput",
    "EmbeddingInput",
    "EmbeddingOutput",
    "ImageGenerationInput",
    "ImageGenerationOutput",
    "ImageDescriptionInput",
    "ImageDescriptionOutput",
    "ModelInvocation",
    "ModelQuotaObservation",
    "ModelUsage",
    "RequestRequirements",
    "ResolvedModelResponse",
    "ResponseMode",
    "SpeechInput",
    "SpeechOutput",
    "TraceContext",
    "TranscriptionInput",
    "TranscriptionOutput",
    "WebSearchHitOutput",
    "WebSearchInput",
    "WebSearchOutput",
]

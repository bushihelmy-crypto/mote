"""Stable contracts for provider-independent model inference."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, JsonValue

from mote.contracts.conversation import Message
from mote.contracts.events.envelope import freeze_json
from mote.contracts.model.invocation import CanonicalToolCall, CanonicalToolDefinition, ResponseMode
from mote.contracts.model.topology import RouteId


class InferenceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    content: str | None = ""
    tool_calls: tuple[CanonicalToolCall, ...] | None = None
    structured_value: JsonValue = None

    @property
    def text(self) -> str:
        return self.content or ""

    @property
    def is_native(self) -> bool:
        return self.tool_calls is not None

    @property
    def is_empty(self) -> bool:
        return not self.content


@dataclass(frozen=True, slots=True)
class InferenceRequirements:
    tool_calling: bool = False
    structured_output: bool = False
    native_schema: bool = False
    multimodal: tuple[str, ...] = ()
    native_tool_search: bool = False
    streaming: bool = True
    continuation: bool = False
    resume: bool = False
    output_representations: tuple[str, ...] = ("text",)


@dataclass(frozen=True, slots=True)
class InferenceIntent:
    model_call_id: str
    requirements: InferenceRequirements
    routing_signals: tuple[tuple[str, str], ...] = ()
    routing_messages: tuple[tuple[str, str], ...] = ()
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.model_call_id:
            raise ValueError("inference intent requires a ModelCall identity")
        if self.estimated_tokens < 0:
            raise ValueError("estimated inference tokens cannot be negative")


@dataclass(frozen=True, slots=True)
class InferenceTargetLease:
    target_id: str
    lease_id: str
    expires_at: float

    def __post_init__(self) -> None:
        if not self.target_id or not self.lease_id:
            raise ValueError("inference target lease requires stable identities")


@dataclass(frozen=True, slots=True)
class InferenceAttemptFence:
    model_call_id: str
    attempt_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not self.model_call_id or not self.attempt_id:
            raise ValueError("inference attempt requires call and attempt identities")
        if self.fencing_token < 1:
            raise ValueError("inference attempt fencing token must be positive")


@dataclass(frozen=True, slots=True)
class EndpointCapabilitySnapshot:
    schema_dialect: str = ""
    schema_constraints_fingerprint: str = ""
    tool_name_limit: int | None = None
    tool_description_limit: int | None = None
    multimodal_envelope: tuple[str, ...] = ()
    structured_output_modes: tuple[str, ...] = ()
    supports_cache: bool = False
    supports_resume: bool = False
    supports_native_tool_search: bool = False
    canonicalization_version: str = "1"


@dataclass(frozen=True, slots=True)
class ResolvedInferenceTarget:
    route_id: RouteId
    command_protocol: str
    command_protocol_version: str
    capabilities: EndpointCapabilitySnapshot
    capability_fingerprint: str
    projection_compatibility_key: str
    lease: InferenceTargetLease


@dataclass(frozen=True, slots=True)
class FinalizedInferenceRequest:
    model_call_id: str
    payload: "FinalizedGenerateRequest"
    protocol_fingerprint: str = ""
    vocabulary_fingerprint: str = ""
    tool_projection_fingerprint: str = ""
    prompt_section_set_fingerprint: str = ""
    request_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.model_call_id:
            raise ValueError("finalized inference request requires a model call identity")
        if not isinstance(self.payload, FinalizedGenerateRequest):
            raise TypeError("finalized inference request only accepts a generate request")


@dataclass(frozen=True, slots=True)
class FinalizedGenerateRequest:
    messages: tuple[Message, ...]
    task: str
    system_prompt: str = ""
    tools: tuple[CanonicalToolDefinition, ...] = ()
    output_schema: Mapping[str, JsonValue] | None = None
    response_mode: ResponseMode = ResponseMode.TEXT
    stream: bool = True
    resume: bool = False
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.messages or any(not isinstance(item, Message) for item in self.messages):
            raise TypeError("finalized generate request requires canonical Message values")
        if not self.task:
            raise ValueError("finalized generate request requires a task identity")
        if any(not isinstance(item, CanonicalToolDefinition) for item in self.tools):
            raise TypeError("finalized generate tools must be canonical definitions")
        if self.output_schema is not None:
            frozen = freeze_json(self.output_schema, path="finalized output schema")
            if not isinstance(frozen, Mapping):
                raise TypeError("finalized output schema must be a JSON object")
            object.__setattr__(self, "output_schema", frozen)


@dataclass(frozen=True, slots=True)
class TargetInvalidated:
    reason: str
    target_id: str
    rebuild_required: bool = True


InferenceOutcome = InferenceResult | TargetInvalidated


__all__ = [
    "EndpointCapabilitySnapshot",
    "FinalizedInferenceRequest",
    "FinalizedGenerateRequest",
    "InferenceAttemptFence",
    "InferenceIntent",
    "InferenceOutcome",
    "InferenceRequirements",
    "InferenceResult",
    "InferenceTargetLease",
    "ResolvedInferenceTarget",
    "TargetInvalidated",
]

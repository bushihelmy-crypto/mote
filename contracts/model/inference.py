"""Stable contracts for provider-independent model inference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mote.contracts.model.topology import RouteId


class CanonicalToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = ""
    command_name: str
    args: dict[str, Any] = Field(default_factory=dict)

    @property
    def call_id(self) -> str:
        return self.id

    @property
    def name(self) -> str:
        return self.command_name

    @property
    def arguments(self) -> dict[str, Any]:
        return self.args

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class InferenceResult(BaseModel):
    content: str | None = ""
    tool_calls: list[dict[str, Any]] | None = None
    structured_value: Any | None = None

    @field_validator("tool_calls", mode="before")
    @classmethod
    def normalize_tool_calls(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = []
        for item in value:
            data = item.model_dump() if isinstance(item, BaseModel) else dict(item)
            data["args"] = data.get("args") or {}
            normalized.append(data)
        return normalized

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


@dataclass(frozen=True, slots=True)
class InferenceTargetLease:
    target_id: str
    lease_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class InferenceAttemptFence:
    model_call_id: str
    attempt_id: str
    fencing_token: int


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
    payload: Any
    messages: tuple[dict[str, Any], ...] = ()
    protocol_fingerprint: str = ""
    vocabulary_fingerprint: str = ""
    tool_projection_fingerprint: str = ""
    prompt_section_set_fingerprint: str = ""
    request_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class TargetInvalidated:
    reason: str
    target_id: str
    rebuild_required: bool = True


InferenceOutcome = InferenceResult | TargetInvalidated


__all__ = [
    "CanonicalToolCall",
    "EndpointCapabilitySnapshot",
    "FinalizedInferenceRequest",
    "InferenceAttemptFence",
    "InferenceIntent",
    "InferenceOutcome",
    "InferenceRequirements",
    "InferenceResult",
    "InferenceTargetLease",
    "ResolvedInferenceTarget",
    "TargetInvalidated",
]

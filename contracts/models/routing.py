"""Versioned, provider-neutral contracts for semantic model routing."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from mote.contracts.models.invocation import ModelOperation, RequestRequirements, TraceContext


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RoutingDegradedReason(StrEnum):
    POLICY_TIMEOUT = "policy_timeout"
    POLICY_ERROR = "policy_error"
    INVALID_PROPOSAL = "invalid_proposal"
    POLICY_UNAVAILABLE = "policy_unavailable"
    ML_UNAVAILABLE = "ml_unavailable"
    NO_ADMISSIBLE_CANDIDATES = "no_admissible_candidates"
    HOLD_INADMISSIBLE = "hold_inadmissible"
    HOLD_EXPIRED = "hold_expired"
    SEED_EXPIRED = "seed_expired"


class RouteCapabilities(_FrozenContract):
    supports_tools: bool = False
    supports_native_schema: bool = False
    supports_server_web_search: bool = False
    supports_vision: bool = False
    supports_pdf: bool = False
    supports_native_tool_search: bool = False


class RouteAdmissionProfile(_FrozenContract):
    """One internally coherent endpoint capability/governance alternative."""

    context_tokens: int = Field(default=0, ge=0)
    capabilities: RouteCapabilities = Field(default_factory=RouteCapabilities)
    governance_domain: str = "default"
    region: str = "global"


class RouteCandidate(_FrozenContract):
    """Secret-free metadata for one logical route intent."""

    route_id: str = Field(min_length=1)
    quality_class: str = Field(min_length=1)
    quality_rank: int = Field(default=1, ge=0)
    cost_class: str = "standard"
    latency_class: str = "standard"
    context_tokens: int = Field(default=0, ge=0)
    capabilities: RouteCapabilities = Field(default_factory=RouteCapabilities)
    admission_profiles: tuple[RouteAdmissionProfile, ...] = ()
    governance_domain: str = "default"
    allowed_regions: frozenset[str] = frozenset()
    data_classifications: frozenset[str] = frozenset({"default"})
    tags: frozenset[str] = frozenset()
    enabled: bool = True


class RoutingMessage(_FrozenContract):
    role: str = Field(min_length=1)
    content: str = ""


class RoutingSignals(_FrozenContract):
    messages: tuple[RoutingMessage, ...] = ()
    prompt_text: str = ""
    estimated_tokens: int = Field(default=0, ge=0)
    conversation_turns: int = Field(default=0, ge=0)
    previous_failures: int = Field(default=0, ge=0)
    previous_assistant_usage: dict[str, JsonValue] | None = None
    flags: frozenset[str] = frozenset()


class RoutingHints(_FrozenContract):
    prefer_cheap: bool = False
    quality_priority: str = "balanced"
    latency_priority: str = "balanced"
    candidate_scope: tuple[str, ...] | None = None


class RoutingInput(_FrozenContract):
    schema_version: Literal[1] = 1
    decision_id: str = Field(min_length=1)
    model_call_id: str = ""
    session_id: str = Field(min_length=1)
    turn_id: int = Field(default=0, ge=0)
    task: str = Field(min_length=1)
    operation: ModelOperation = ModelOperation.GENERATE
    requirements: RequestRequirements = Field(default_factory=RequestRequirements)
    signals: RoutingSignals = Field(default_factory=RoutingSignals)
    caller_hints: RoutingHints = Field(default_factory=RoutingHints)
    trace: TraceContext = Field(default_factory=TraceContext)


class CandidateScore(_FrozenContract):
    route_id: str = Field(min_length=1)
    score: float


class RecentRoutingDecision(_FrozenContract):
    decision_id: str = Field(min_length=1)
    selected_route_id: str = Field(min_length=1)
    final_class: str | None = None
    turn_id: int = Field(default=0, ge=0)


class RoutingHold(_FrozenContract):
    target_route_id: str = Field(min_length=1)
    evidence: str = ""
    turns_remaining: int | None = Field(default=None, ge=1)
    expires_at_utc: AwareDatetime | None = None


class SeedFloor(_FrozenContract):
    route_class: str = Field(min_length=1)
    turns_remaining: int | None = Field(default=None, ge=1)
    expires_at_utc: AwareDatetime | None = None


class RoutingSessionState(_FrozenContract):
    schema_version: Literal[1] = 1
    generation: int = Field(default=0, ge=0)
    recent_decisions: tuple[RecentRoutingDecision, ...] = ()
    seed_floor: SeedFloor | None = None
    control_hold: RoutingHold | None = None


class RoutingStateTransition(_FrozenContract):
    append_final_class: str | None = None
    consume_hold: bool = False
    clear_hold: bool = False
    consume_seed: bool = False
    clear_seed: bool = False


class RoutingProposal(_FrozenContract):
    selected_route_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    feature_schema_revision: str = Field(min_length=1)
    base_class: str | None = None
    final_class: str | None = None
    base_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    scores: tuple[CandidateScore, ...] = ()
    reason_codes: tuple[str, ...] = ()
    explanation: str = ""
    selection_kind: str = "score"
    degraded_reason: RoutingDegradedReason | None = None
    state_transition: RoutingStateTransition = Field(default_factory=RoutingStateTransition)


class RoutingDecision(_FrozenContract):
    schema_version: Literal[1] = 1
    decision_id: str = Field(min_length=1)
    model_call_id: str = ""
    selected_route_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    feature_schema_revision: str = Field(min_length=1)
    catalog_revision: str = Field(min_length=1)
    state_generation: int = Field(ge=1)
    status: Literal["selected", "fallback", "held"]
    degraded_reason: RoutingDegradedReason | None = None
    base_class: str | None = None
    final_class: str | None = None
    base_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()
    selection_kind: str = "score"
    candidate_scores: tuple[CandidateScore, ...] = ()
    latency_ms: float = Field(ge=0.0)


__all__ = [
    "CandidateScore",
    "RecentRoutingDecision",
    "RouteCandidate",
    "RouteAdmissionProfile",
    "RouteCapabilities",
    "RoutingDecision",
    "RoutingDegradedReason",
    "RoutingHints",
    "RoutingHold",
    "RoutingInput",
    "RoutingMessage",
    "RoutingProposal",
    "RoutingSessionState",
    "RoutingSignals",
    "RoutingStateTransition",
    "SeedFloor",
]

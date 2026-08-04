"""Domain-owned event contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional, cast

from mote.contracts.events._base import DurableFact as _DurableFact
from mote.contracts.events.envelope import JsonValue, freeze_json

if TYPE_CHECKING:
    pass

LLM_STREAM_DELTA = "llm_stream_delta"

LLM_STREAM_COMMITTED = "llm_stream_committed"

LLM_STREAM_DISCARDED = "llm_stream_discarded"

LLM_STREAM_INTERRUPTED = "llm_stream_interrupted"

LLM_STREAM_END = "llm_stream_end"

MODEL_CALL_PLANNED = "model_call_planned"

MODEL_ATTEMPT_ADMISSION_REJECTED = "model_attempt_admission_rejected"

MODEL_ATTEMPT_STARTED = "model_attempt_started"

MODEL_ATTEMPT_FINISHED = "model_attempt_finished"

MODEL_FALLBACK_SELECTED = "model_fallback_selected"

MODEL_CALL_FINISHED = "model_call_finished"

ROUTING_DECISION = "routing_decision"

BREAKER_STATE_CHANGE = "breaker_state_change"


@dataclass
class LLMStreamDeltaEvent:
    """One streamed token (or chunk) from the LLM client."""

    token: str = ""
    model_call_id: str = ""
    attempt_id: str = ""
    sequence: int = 0
    provisional: bool = False

    name: ClassVar[str] = LLM_STREAM_DELTA


@dataclass
class LLMStreamCommittedEvent:
    """One accepted attempt's buffered deltas became visible output."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0

    name: ClassVar[str] = LLM_STREAM_COMMITTED


@dataclass
class LLMStreamDiscardedEvent:
    """One failed attempt's provisional deltas were permanently rejected."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = "attempt_failed"

    name: ClassVar[str] = LLM_STREAM_DISCARDED


@dataclass
class LLMStreamInterruptedEvent:
    """A cancelled logical call ended with an uncommitted attempt stream."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = "cancelled"

    name: ClassVar[str] = LLM_STREAM_INTERRUPTED


@dataclass
class LLMStreamEndEvent:
    """The current LLM stream finished (turn boundary for the renderer)."""

    name: ClassVar[str] = LLM_STREAM_END


@dataclass
class ModelCallPlannedEvent:
    model_call_id: str = ""
    routing_decision_id: str = ""
    plan_id: str = ""
    route_id: str = ""
    route_schema_version: int = 2
    runtime_generation_id: str = ""
    topology_revision: str = ""
    config_revision: str = ""
    policy_id: str = ""
    resume_generation: int = 0
    endpoint_ids: List[str] = field(default_factory=list)
    budget: Mapping[str, JsonValue] = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_PLANNED

    def __post_init__(self) -> None:
        budget = freeze_json(self.budget, path="model call budget")
        if not isinstance(budget, Mapping):
            raise TypeError("model call budget must be an object")
        self.budget = cast(Mapping[str, JsonValue], budget)


@dataclass
class ModelAttemptAdmissionRejectedEvent:
    model_call_id: str = ""
    resume_generation: int = 0
    endpoint_id: str = ""
    credential_slot_id: str = ""
    gate: str = ""
    reason: str = ""
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_ADMISSION_REJECTED


@dataclass
class ModelAttemptStartedEvent:
    model_call_id: str = ""
    attempt_id: str = ""
    ordinal: int = 0
    resume_generation: int = 0
    endpoint_id: str = ""
    credential_slot_id: str = ""
    model: str = ""
    provider: str = ""
    input: JsonValue = None
    timeout_seconds: float = 0.0
    parent_span_id: Optional[str] = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_STARTED

    def __post_init__(self) -> None:
        self.input = freeze_json(self.input, path="model attempt input")


@dataclass
class ModelAttemptFinishedEvent:
    model_call_id: str = ""
    attempt_id: str = ""
    ordinal: int = 0
    resume_generation: int = 0
    endpoint_id: str = ""
    state: str = ""
    failure_reason: str = ""
    latency_ms: float = 0.0
    usage: Mapping[str, JsonValue] = field(default_factory=dict)
    cost_usd: float = 0.0
    output: JsonValue = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_FINISHED

    def __post_init__(self) -> None:
        usage = freeze_json(self.usage, path="model attempt usage")
        if not isinstance(usage, Mapping):
            raise TypeError("model attempt usage must be an object")
        self.usage = cast(Mapping[str, JsonValue], usage)
        self.output = freeze_json(self.output, path="model attempt output")


@dataclass
class ModelFallbackSelectedEvent:
    model_call_id: str = ""
    resume_generation: int = 0
    from_endpoint_id: str = ""
    to_endpoint_id: str = ""
    reason: str = ""
    wire_attempts_used: int = 0
    trace_id: str = ""

    name: ClassVar[str] = MODEL_FALLBACK_SELECTED


@dataclass
class ModelCallFinishedEvent:
    model_call_id: str = ""
    state: str = ""
    selected_endpoint_id: str = ""
    wire_attempts: int = 0
    usage: Mapping[str, JsonValue] = field(default_factory=dict)
    cost_usd: float = 0.0
    summary: Mapping[str, JsonValue] = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_FINISHED

    def __post_init__(self) -> None:
        for name in ("usage", "summary"):
            frozen = freeze_json(getattr(self, name), path=f"model call {name}")
            if not isinstance(frozen, Mapping):
                raise TypeError(f"model call {name} must be an object")
            setattr(self, name, cast(Mapping[str, JsonValue], frozen))


@dataclass(frozen=True)
class RoutingDecisionEvent(_DurableFact):
    """A guarded semantic route decision committed before model execution."""

    decision: Mapping[str, JsonValue] = field(default_factory=dict)
    state: Mapping[str, JsonValue] = field(default_factory=dict)
    route_schema_version: int = 2

    name: ClassVar[str] = ROUTING_DECISION
    type: ClassVar[str] = ROUTING_DECISION

    def __post_init__(self) -> None:
        for name in ("decision", "state"):
            frozen = freeze_json(getattr(self, name), path=f"routing {name}")
            if not isinstance(frozen, Mapping):
                raise TypeError(f"routing {name} must be an object")
            object.__setattr__(self, name, frozen)

    def payload(self) -> dict[str, JsonValue]:
        return {
            "decision": dict(self.decision),
            "state": dict(self.state),
            "route_schema_version": self.route_schema_version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "RoutingDecisionEvent":
        if set(payload) != {"decision", "state", "route_schema_version"}:
            raise ValueError(f"{cls.__name__} payload fields are not canonical")
        decision = payload["decision"]
        state = payload["state"]
        route_schema_version = payload["route_schema_version"]
        if type(decision) is not dict or type(state) is not dict:
            raise TypeError("routing decision and state must be objects")
        if type(route_schema_version) is not int:
            raise TypeError("routing route_schema_version must be an integer")
        return cls(
            decision=decision,
            state=state,
            route_schema_version=route_schema_version,
        )


@dataclass
class BreakerStateChangeEvent:
    """A resource's :class:`~mote.runtime.resilience.CircuitBreaker` changed state.

    Emitted (observation-only) when a breaker transitions closed→open,
    open→half_open, or half_open→closed/open, so a frontend/logger can see a
    provider being shed and recovering. The breaker's own ``admit``/``record``
    verdicts are the source of truth; this just mirrors *that a resource's health
    state flipped*. ``key`` is the opaque resource label (for LLM:
    ``api_type::model::key_index``); ``reason`` is the breaker's human note.
    """

    key: str = ""
    old_state: str = ""  # BreakerState.value
    new_state: str = ""
    reason: str = ""

    name: ClassVar[str] = BREAKER_STATE_CHANGE

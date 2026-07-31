"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

from mote.contracts.events._base import DurableFact as _DurableFact

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
    budget: dict = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_PLANNED


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
    input: Any = None
    timeout_seconds: float = 0.0
    parent_span_id: Optional[str] = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_STARTED


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
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    output: Any = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_FINISHED


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
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    summary: dict = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_FINISHED


@dataclass
class RoutingDecisionEvent(_DurableFact):
    """A guarded semantic route decision committed before model execution."""

    decision: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    route_schema_version: int = 2

    name: ClassVar[str] = ROUTING_DECISION
    type: ClassVar[str] = ROUTING_DECISION


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

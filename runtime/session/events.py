"""Session rollout event schema — the on-disk record types.

Each line of a ``rollout.jsonl`` is one JSON object::

    {"type": <event-type>, "ts": <iso8601>, "payload": {...}}

The event set is a small tagged union (Codex ``RolloutItem`` style). Recoverable
facts are committed explicitly through ``SessionFactCommitter`` before their
best-effort telemetry observations are emitted:

* ``message`` / ``context_compacted`` / ``history_edited`` originate from the
  context layer;
* ``turn_context`` originates from the Role lifecycle;
* ``session_meta`` is the first fact committed for a fresh session.

All three paths use the same session committer, so rollout identity and history
have one durable source of truth.
``schema_version`` guards future migrations. Message payloads reuse ``Message.dump()`` (parsed to a
dict) so they round-trip losslessly through ``Message.load()`` on resume.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Union

from mote.contracts.conversation import Message
from mote.contracts.events.conversation import PROMPT_REJECTED, PromptRejectedEvent
from mote.contracts.events.file.facts import (
    FILE_EDIT_PLAN_STORED,
    FILE_HISTORY_IMPORTED,
    FILE_TRANSACTION_ABORTED,
    FILE_TRANSACTION_COMMITTED,
    FILE_TRANSACTION_IN_DOUBT,
    FILE_TRANSACTION_PREPARED,
    HUNK_DETECTED,
    HUNK_REVIEW_TRANSITIONED,
    REWIND_ABORTED,
    REWIND_COMMITTED,
    REWIND_IN_DOUBT,
    REWIND_PREPARED,
    FileEditPlanStoredEvent,
    FileHistoryImportedEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)
from mote.contracts.events.model import ROUTING_DECISION
from mote.contracts.events.output import (
    OUTPUT_ACCEPTED,
    OUTPUT_CANDIDATE_RECEIVED,
    OUTPUT_COMMIT_STARTED,
    OUTPUT_COMMITTED,
    OUTPUT_MIGRATED,
    OUTPUT_PUBLICATION_QUEUED,
    OUTPUT_PUBLISHED,
    OUTPUT_VALIDATION_REJECTED,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
)
from mote.contracts.model.failover import ModelCallSummary
from mote.contracts.runtime import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
)
from mote.contracts.runtime.handoff import RuntimeHandoffIntent, RuntimeHandoffResolution
from mote.contracts.tool import ToolsetManifest, parse_toolset_manifest


def _dataclass_kwargs(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate that ``payload`` exactly matches fields owned by ``cls``."""
    names = {f.name for f in fields(cls)}
    if set(payload) != names:
        raise ValueError(f"{cls.__name__} payload fields must be exactly {sorted(names)!r}")
    return dict(payload)


def _require_keys(payload: Dict[str, Any], names: set[str], owner: str) -> Dict[str, Any]:
    if set(payload) != names:
        raise ValueError(f"{owner} payload fields must be exactly {sorted(names)!r}")
    return payload


_RUNTIME_CHECKPOINT_FIELDS = {
    "runtime_id",
    "kind",
    "epoch",
    "revision",
    "codec",
    "schema_version",
    "payload_ref",
    "alias",
    "digest",
    "sensitivity",
    "fidelity",
}


def _decode_runtime_checkpoint(payload: object, *, owner: str) -> RuntimeCheckpoint:
    if type(payload) is not dict:
        raise TypeError(f"{owner} payload must be an object")
    _require_keys(payload, _RUNTIME_CHECKPOINT_FIELDS, owner)
    string_fields = (
        "runtime_id",
        "kind",
        "codec",
        "payload_ref",
        "alias",
        "digest",
        "sensitivity",
        "fidelity",
    )
    for field_name in string_fields:
        if type(payload[field_name]) is not str:
            raise TypeError(f"{owner}.{field_name} must be a string")
    for field_name in ("epoch", "revision", "schema_version"):
        if type(payload[field_name]) is not int:
            raise TypeError(f"{owner}.{field_name} must be an integer")
    return RuntimeCheckpoint(
        runtime_id=payload["runtime_id"],
        kind=payload["kind"],
        epoch=payload["epoch"],
        revision=payload["revision"],
        codec=payload["codec"],
        schema_version=payload["schema_version"],
        payload_ref=payload["payload_ref"],
        alias=payload["alias"],
        digest=payload["digest"],
        sensitivity=payload["sensitivity"],
        fidelity=CheckpointFidelity(payload["fidelity"]),
    )


#: Bump when the persisted event shape changes incompatibly (drives migration).
SCHEMA_VERSION = 3


def _now_iso() -> str:
    return datetime.now().isoformat()


def _message_to_payload(message: Message) -> Dict[str, Any]:
    """Serialize a Message to a JSON-object payload (round-trips via from_dict).

    ``mode="json"`` yields JSON-native types directly (running the same field
    serializers as :meth:`Message.dump`), so we skip the ``dump``→``json.loads``
    string round-trip.
    """
    return message.model_dump(mode="json", exclude_none=True, warnings=False)


def _payload_to_message(payload: Dict[str, Any]) -> Message:
    """Reconstruct one current Message payload."""
    return Message.from_dict(payload)


# ---------------------------------------------------------------------------
# Event types (tagged union, discriminated by ``type``)
# ---------------------------------------------------------------------------

#: Event-type discriminators.
SESSION_META = "session_meta"
MESSAGE = "message"
CONTEXT_COMPACTED = "context_compacted"
HISTORY_EDITED = "history_edited"
TURN_CONTEXT = "turn_context"
META_UPDATE = "meta_update"
CHECKPOINT = "checkpoint"
LLM_CALL = "llm_call"
ROUTING_DECISION_FACT = ROUTING_DECISION
RUNTIME_CHECKPOINT = "runtime_checkpoint"
RUNTIME_COMMIT = "runtime_commit"
RUNTIME_PROJECTION_ACKNOWLEDGED = "runtime_projection_acknowledged"
RUNTIME_OPERATION_PREPARED = "runtime_operation_prepared"
RUNTIME_OPERATION_COMPLETED = "runtime_operation_completed"
RUNTIME_OPERATION_ABORTED = "runtime_operation_aborted"
RUNTIME_HANDOFF_PREPARED = "runtime_handoff_prepared"
RUNTIME_HANDOFF_ACTIVATED = "runtime_handoff_activated"
RUNTIME_HANDOFF_RESOLVED = "runtime_handoff_resolved"


@dataclass
class SessionMetaEvent:
    """First-line metadata identifying the session (Codex ``SessionMeta``)."""

    session_id: str
    role_class: str
    toolset_manifest: ToolsetManifest
    schema_version: int = SCHEMA_VERSION
    parent_session_id: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    working_dir: str = ""
    original_working_dir: str = ""
    project_root: str = ""
    model: Optional[str] = None

    type = SESSION_META

    def payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["toolset_manifest"] = [identity.to_payload() for identity in self.toolset_manifest]
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionMetaEvent":
        values = _dataclass_kwargs(cls, payload)
        values["toolset_manifest"] = parse_toolset_manifest(values["toolset_manifest"])
        return cls(**values)


@dataclass
class MessageEvent:
    """A single message appended to the stored history."""

    message: Message

    type = MESSAGE

    def payload(self) -> Dict[str, Any]:
        return _message_to_payload(self.message)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "MessageEvent":
        return cls(message=_payload_to_message(payload))


@dataclass
class ContextCompactedFact:
    """A committed transition of the model-context projection only."""

    model_context_messages: List[Message]
    source_message_ids: List[str]
    summary: str = ""
    strategy: str = ""
    trigger: str = "auto"

    type = CONTEXT_COMPACTED

    def payload(self) -> Dict[str, Any]:
        return {
            "model_context": [_message_to_payload(message) for message in self.model_context_messages],
            "source_message_ids": list(self.source_message_ids),
            "summary": self.summary,
            "strategy": self.strategy,
            "trigger": self.trigger,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "ContextCompactedFact":
        _require_keys(
            payload,
            {"model_context", "source_message_ids", "summary", "strategy", "trigger"},
            cls.__name__,
        )
        messages = [_payload_to_message(item) for item in payload["model_context"]]
        return cls(
            model_context_messages=messages,
            source_message_ids=[str(message_id) for message_id in payload["source_message_ids"]],
            summary=str(payload["summary"]),
            strategy=str(payload["strategy"]),
            trigger=str(payload["trigger"]),
        )


@dataclass
class HistoryEditedFact:
    """A user-authored removal applied to transcript and model projections."""

    removed_message_ids: List[str]
    clear_all: bool = False
    reason: str = "delete"

    type = HISTORY_EDITED

    def payload(self) -> Dict[str, Any]:
        return {
            "removed_message_ids": list(self.removed_message_ids),
            "clear_all": self.clear_all,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "HistoryEditedFact":
        _require_keys(payload, {"removed_message_ids", "clear_all", "reason"}, cls.__name__)
        return cls(
            removed_message_ids=[str(message_id) for message_id in payload["removed_message_ids"]],
            clear_all=payload["clear_all"],
            reason=str(payload["reason"]),
        )


@dataclass
class TurnContextEvent:
    """Per-turn runtime snapshot written at the turn boundary."""

    turn_id: str
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[Dict[str, Any]] = None

    type = TURN_CONTEXT

    def payload(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "working_dir": self.working_dir,
            "model": self.model,
            "token_state": self.token_state,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TurnContextEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class MetaUpdateEvent:
    """Mutable metadata appended at the tail for fast session listing."""

    title: Optional[str] = None
    last_prompt: Optional[str] = None

    type = META_UPDATE

    def payload(self) -> Dict[str, Any]:
        return {"title": self.title, "last_prompt": self.last_prompt}

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "MetaUpdateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class CheckpointEvent:
    """A whole-working-tree checkpoint captured at a user-turn boundary.

    Captures the *entire* working tree once per user prompt so the user can roll
    the whole tree back to any prior turn (the ``/rewind`` command).

    ``commit`` is a commit id in the session's dedicated ``{session}/git`` object
    db (never the user's own repo). ``prompt_index`` is the 0-based ordinal of the
    user turn this snapshot precedes; ``prompt_preview`` is the leading slice of
    that prompt (for the ``/rewind`` listing), and ``working_dir`` is the tree
    root the checkpoint was taken against.

    ``after_commit`` is the twin snapshot taken at *turn end* — the tree the agent
    left behind after acting on this prompt. It is recorded as a separate event
    (``commit=""`` + this field set, same ``prompt_index``) so
    :func:`~mote.runtime.session.checkpoint.list_checkpoints` can fold it back onto the
    matching before-checkpoint. Diffing it against the live tree at rewind time is
    how the ``/rewind`` command detects files an external process (or the user)
    changed *after* the agent finished — the whole-tree analogue of grok's
    per-file ``after_snapshots`` conflict detection.
    """

    commit: str
    prompt_index: int = 0
    prompt_preview: str = ""
    working_dir: str = ""
    after_commit: str = ""

    type = CHECKPOINT

    def payload(self) -> Dict[str, Any]:
        return {
            "commit": self.commit,
            "prompt_index": self.prompt_index,
            "prompt_preview": self.prompt_preview,
            "working_dir": self.working_dir,
            "after_commit": self.after_commit,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "CheckpointEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class LLMCallEvent:
    """A single LLM completion's token usage + cost (compact telemetry record).

    Persisted per LLM call so a rollout carries per-request token/cost without
    duplicating the prompt/completion (those already land as ``message`` records
    and as the live ``MessageAppendedEvent`` stream). Purely telemetry: ignored
    by :func:`~mote.runtime.session.replay.replay` (not part of the history rebuild).
    """

    request_id: str = ""
    model: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    summary: Optional[ModelCallSummary] = None

    type = LLM_CALL

    def payload(self) -> Dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "model": self.model,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "summary": (self.summary.model_dump(mode="json") if self.summary is not None else None),
        }
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "LLMCallEvent":
        values = _dataclass_kwargs(cls, payload)
        summary = values.get("summary")
        if isinstance(summary, dict):
            values["summary"] = ModelCallSummary.model_validate(summary)
        return cls(**values)


@dataclass
class RoutingDecisionFact:
    """Recoverable semantic routing state transition and audit record."""

    decision: Dict[str, Any]
    state: Dict[str, Any]
    route_schema_version: int = 2

    type = ROUTING_DECISION_FACT

    def payload(self) -> Dict[str, Any]:
        return {
            "decision": dict(self.decision),
            "state": dict(self.state),
            "route_schema_version": self.route_schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RoutingDecisionFact":
        _require_keys(payload, {"decision", "state", "route_schema_version"}, cls.__name__)
        return cls(
            decision=dict(payload["decision"]),
            state=dict(payload["state"]),
            route_schema_version=int(payload["route_schema_version"]),
        )


@dataclass
class RuntimeCheckpointEvent:
    """One recoverable checkpoint emitted by a managed Runtime."""

    checkpoint: RuntimeCheckpoint
    reason: str = ""

    type = RUNTIME_CHECKPOINT

    def payload(self) -> Dict[str, Any]:
        checkpoint = self.checkpoint
        return {
            "runtime_id": checkpoint.runtime_id,
            "kind": checkpoint.kind,
            "epoch": checkpoint.epoch,
            "revision": checkpoint.revision,
            "codec": checkpoint.codec,
            "schema_version": checkpoint.schema_version,
            "payload_ref": checkpoint.payload_ref,
            "alias": checkpoint.alias,
            "digest": checkpoint.digest,
            "sensitivity": checkpoint.sensitivity,
            "fidelity": checkpoint.fidelity.value,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeCheckpointEvent":
        _require_keys(
            payload,
            {
                "runtime_id",
                "kind",
                "epoch",
                "revision",
                "codec",
                "schema_version",
                "payload_ref",
                "alias",
                "digest",
                "sensitivity",
                "fidelity",
                "reason",
            },
            cls.__name__,
        )
        checkpoint_payload = {key: value for key, value in payload.items() if key != "reason"}
        if type(payload["reason"]) is not str:
            raise TypeError(f"{cls.__name__}.reason must be a string")
        return cls(
            checkpoint=_decode_runtime_checkpoint(checkpoint_payload, owner=cls.__name__),
            reason=payload["reason"],
        )


@dataclass
class RuntimeCommitEvent:
    """One durable Runtime checkpoint plus its replayable projection intents."""

    fact: RuntimeCommitFact

    type = RUNTIME_COMMIT

    def payload(self) -> Dict[str, Any]:
        checkpoint = self.fact.checkpoint
        return {
            "commit_id": self.fact.commit_id,
            "checkpoint": {
                "runtime_id": checkpoint.runtime_id,
                "kind": checkpoint.kind,
                "epoch": checkpoint.epoch,
                "revision": checkpoint.revision,
                "codec": checkpoint.codec,
                "schema_version": checkpoint.schema_version,
                "payload_ref": checkpoint.payload_ref,
                "alias": checkpoint.alias,
                "digest": checkpoint.digest,
                "sensitivity": checkpoint.sensitivity,
                "fidelity": checkpoint.fidelity.value,
            },
            "projections": [
                {
                    "intent_id": intent.intent_id,
                    "projector": intent.projector,
                    "schema_version": intent.schema_version,
                    "options": [list(item) for item in intent.options],
                }
                for intent in self.fact.projections
            ],
            "reason": self.fact.reason,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeCommitEvent":
        _require_keys(payload, {"commit_id", "checkpoint", "projections", "reason"}, cls.__name__)
        checkpoint = payload["checkpoint"]
        _require_keys(
            checkpoint,
            {
                "runtime_id",
                "kind",
                "epoch",
                "revision",
                "codec",
                "schema_version",
                "payload_ref",
                "alias",
                "digest",
                "sensitivity",
                "fidelity",
            },
            f"{cls.__name__}.checkpoint",
        )
        for item in payload["projections"]:
            _require_keys(
                item,
                {"intent_id", "projector", "schema_version", "options"},
                f"{cls.__name__}.projection",
            )
        return cls(
            fact=RuntimeCommitFact(
                commit_id=str(payload["commit_id"]),
                checkpoint=_decode_runtime_checkpoint(checkpoint, owner=f"{cls.__name__}.checkpoint"),
                projections=tuple(
                    RuntimeProjectionIntent(
                        intent_id=str(item["intent_id"]),
                        projector=str(item["projector"]),
                        schema_version=int(item["schema_version"]),
                        options=tuple((str(option[0]), str(option[1])) for option in item["options"]),
                    )
                    for item in payload["projections"]
                ),
                reason=str(payload["reason"]),
            )
        )


@dataclass
class RuntimeProjectionAcknowledgedEvent:
    """A durable acknowledgement for one Runtime projection request."""

    ack: RuntimeProjectionAck

    type = RUNTIME_PROJECTION_ACKNOWLEDGED

    def payload(self) -> Dict[str, Any]:
        return {
            "commit_id": self.ack.commit_id,
            "intent_id": self.ack.intent_id,
            "status": self.ack.status,
            "error": self.ack.error,
            "attempts": self.ack.attempts,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeProjectionAcknowledgedEvent":
        _require_keys(
            payload,
            {"commit_id", "intent_id", "status", "error", "attempts"},
            cls.__name__,
        )
        return cls(
            ack=RuntimeProjectionAck(
                commit_id=str(payload["commit_id"]),
                intent_id=str(payload["intent_id"]),
                status=str(payload["status"]),
                error=str(payload["error"]),
                attempts=int(payload["attempts"]),
            )
        )


@dataclass
class RuntimeOperationPreparedEvent:
    """A deterministic Runtime mutation durably recorded before application."""

    intent: RuntimeOperationIntent

    type = RUNTIME_OPERATION_PREPARED

    def payload(self) -> Dict[str, Any]:
        intent = self.intent
        checkpoint = intent.base_checkpoint
        return {
            "operation_id": intent.operation_id,
            "runtime_id": intent.runtime_id,
            "kind": intent.kind,
            "alias": intent.alias,
            "epoch": intent.epoch,
            "base_revision": intent.base_revision,
            "target_revision": intent.target_revision,
            "codec": intent.codec,
            "schema_version": intent.schema_version,
            "operation_payload": intent.payload,
            "base_checkpoint": {
                "runtime_id": checkpoint.runtime_id,
                "kind": checkpoint.kind,
                "epoch": checkpoint.epoch,
                "revision": checkpoint.revision,
                "codec": checkpoint.codec,
                "schema_version": checkpoint.schema_version,
                "payload_ref": checkpoint.payload_ref,
                "alias": checkpoint.alias,
                "digest": checkpoint.digest,
                "sensitivity": checkpoint.sensitivity,
                "fidelity": checkpoint.fidelity.value,
            },
            "projections": [
                {
                    "intent_id": projection.intent_id,
                    "projector": projection.projector,
                    "schema_version": projection.schema_version,
                    "options": [list(item) for item in projection.options],
                }
                for projection in intent.projections
            ],
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeOperationPreparedEvent":
        _require_keys(
            payload,
            {
                "operation_id",
                "runtime_id",
                "kind",
                "alias",
                "epoch",
                "base_revision",
                "target_revision",
                "codec",
                "schema_version",
                "operation_payload",
                "base_checkpoint",
                "projections",
            },
            cls.__name__,
        )
        checkpoint = payload["base_checkpoint"]
        _require_keys(
            checkpoint,
            {
                "runtime_id",
                "kind",
                "epoch",
                "revision",
                "codec",
                "schema_version",
                "payload_ref",
                "alias",
                "digest",
                "sensitivity",
                "fidelity",
            },
            f"{cls.__name__}.base_checkpoint",
        )
        for item in payload["projections"]:
            _require_keys(
                item,
                {"intent_id", "projector", "schema_version", "options"},
                f"{cls.__name__}.projection",
            )
        return cls(
            intent=RuntimeOperationIntent(
                operation_id=str(payload["operation_id"]),
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                alias=str(payload["alias"]),
                epoch=int(payload["epoch"]),
                base_revision=int(payload["base_revision"]),
                target_revision=int(payload["target_revision"]),
                codec=str(payload["codec"]),
                schema_version=int(payload["schema_version"]),
                payload=str(payload["operation_payload"]),
                base_checkpoint=_decode_runtime_checkpoint(checkpoint, owner=f"{cls.__name__}.base_checkpoint"),
                projections=tuple(
                    RuntimeProjectionIntent(
                        intent_id=str(item["intent_id"]),
                        projector=str(item["projector"]),
                        schema_version=int(item["schema_version"]),
                        options=tuple((str(option[0]), str(option[1])) for option in item["options"]),
                    )
                    for item in payload["projections"]
                ),
            )
        )


@dataclass
class RuntimeOperationCompletedEvent:
    operation_id: str
    receipt: RuntimeOperationReceipt | None = None

    type = RUNTIME_OPERATION_COMPLETED

    def payload(self) -> Dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "receipt": asdict(self.receipt) if self.receipt is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeOperationCompletedEvent":
        _require_keys(payload, {"operation_id", "receipt"}, cls.__name__)
        raw_receipt = payload["receipt"]
        receipt = RuntimeOperationReceipt(**raw_receipt) if isinstance(raw_receipt, dict) else None
        return cls(operation_id=str(payload["operation_id"]), receipt=receipt)


@dataclass
class RuntimeOperationAbortedEvent:
    operation_id: str

    type = RUNTIME_OPERATION_ABORTED

    def payload(self) -> Dict[str, Any]:
        return {"operation_id": self.operation_id}

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeOperationAbortedEvent":
        _require_keys(payload, {"operation_id"}, cls.__name__)
        return cls(operation_id=str(payload["operation_id"]))


@dataclass
class RuntimeHandoffPreparedEvent:
    """Ownership handoff durably prepared before the surface becomes writable."""

    intent: RuntimeHandoffIntent

    type = RUNTIME_HANDOFF_PREPARED

    def payload(self) -> Dict[str, Any]:
        intent = self.intent
        checkpoint = intent.base_checkpoint
        return {
            "handoff_id": intent.handoff_id,
            "runtime_id": intent.runtime_id,
            "kind": intent.kind,
            "alias": intent.alias,
            "epoch": intent.epoch,
            "base_revision": intent.base_revision,
            "target_revision": intent.target_revision,
            "owner_id": intent.owner_id,
            "fencing_token": intent.fencing_token,
            "mode": intent.mode,
            "message": intent.message,
            "selection": list(intent.selection),
            "base_checkpoint": (RuntimeCheckpointEvent(checkpoint).payload() if checkpoint is not None else None),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeHandoffPreparedEvent":
        _require_keys(
            payload,
            {
                "handoff_id",
                "runtime_id",
                "kind",
                "alias",
                "epoch",
                "base_revision",
                "target_revision",
                "owner_id",
                "fencing_token",
                "mode",
                "message",
                "selection",
                "base_checkpoint",
            },
            cls.__name__,
        )
        checkpoint_payload = payload["base_checkpoint"]
        checkpoint = (
            RuntimeCheckpointEvent.from_payload(checkpoint_payload).checkpoint
            if isinstance(checkpoint_payload, dict)
            else None
        )
        return cls(
            intent=RuntimeHandoffIntent(
                handoff_id=str(payload["handoff_id"]),
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                alias=str(payload["alias"]),
                epoch=int(payload["epoch"]),
                base_revision=int(payload["base_revision"]),
                target_revision=int(payload["target_revision"]),
                owner_id=str(payload["owner_id"]),
                fencing_token=int(payload["fencing_token"]),
                mode=str(payload["mode"]),
                message=str(payload["message"]),
                selection=tuple(str(item) for item in payload["selection"]),
                base_checkpoint=checkpoint,
            )
        )


@dataclass
class RuntimeHandoffActivatedEvent:
    """The prepared handoff surface has obtained human write authority."""

    handoff_id: str

    type = RUNTIME_HANDOFF_ACTIVATED

    def payload(self) -> Dict[str, Any]:
        return {"handoff_id": self.handoff_id}

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeHandoffActivatedEvent":
        _require_keys(payload, {"handoff_id"}, cls.__name__)
        return cls(handoff_id=str(payload["handoff_id"]))


@dataclass
class RuntimeHandoffResolvedEvent:
    """Terminal handoff state, optionally carrying its final checkpoint."""

    resolution: RuntimeHandoffResolution

    type = RUNTIME_HANDOFF_RESOLVED

    def payload(self) -> Dict[str, Any]:
        resolution = self.resolution
        return {
            "handoff_id": resolution.handoff_id,
            "status": resolution.status,
            "runtime_id": resolution.runtime_id,
            "kind": resolution.kind,
            "alias": resolution.alias,
            "epoch": resolution.epoch,
            "revision": resolution.revision,
            "checkpoint": (
                RuntimeCheckpointEvent(resolution.checkpoint).payload() if resolution.checkpoint is not None else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeHandoffResolvedEvent":
        _require_keys(
            payload,
            {
                "handoff_id",
                "status",
                "runtime_id",
                "kind",
                "alias",
                "epoch",
                "revision",
                "checkpoint",
            },
            cls.__name__,
        )
        checkpoint_payload = payload["checkpoint"]
        return cls(
            resolution=RuntimeHandoffResolution(
                handoff_id=str(payload["handoff_id"]),
                status=str(payload["status"]),
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                alias=str(payload["alias"]),
                epoch=int(payload["epoch"]),
                revision=int(payload["revision"]),
                checkpoint=(
                    RuntimeCheckpointEvent.from_payload(checkpoint_payload).checkpoint
                    if isinstance(checkpoint_payload, dict)
                    else None
                ),
            )
        )


#: Any concrete event (tagged union; every member exposes ``.type`` and
#: ``.payload()`` for writing, and a ``from_payload`` classmethod for reading).
SessionEvent = Union[
    SessionMetaEvent,
    MessageEvent,
    ContextCompactedFact,
    HistoryEditedFact,
    TurnContextEvent,
    PromptRejectedEvent,
    MetaUpdateEvent,
    FileHistoryImportedEvent,
    FileEditPlanStoredEvent,
    CheckpointEvent,
    LLMCallEvent,
    RoutingDecisionFact,
    RuntimeCheckpointEvent,
    RuntimeCommitEvent,
    RuntimeProjectionAcknowledgedEvent,
    RuntimeOperationPreparedEvent,
    RuntimeOperationCompletedEvent,
    RuntimeOperationAbortedEvent,
    RuntimeHandoffPreparedEvent,
    RuntimeHandoffActivatedEvent,
    RuntimeHandoffResolvedEvent,
    OutputCandidateReceivedEvent,
    OutputValidationRejectedEvent,
    OutputAcceptedEvent,
    OutputCommitStartedEvent,
    OutputMigratedEvent,
    OutputCommittedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    FileTransactionPreparedEvent,
    FileTransactionCommittedEvent,
    FileTransactionAbortedEvent,
    FileTransactionInDoubtEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
    RewindPreparedEvent,
    RewindCommittedEvent,
    RewindAbortedEvent,
    RewindInDoubtEvent,
]

#: Historic discriminator -> current event payload class. The v3 session codec
#: owns stable persisted names; this map owns typed payload reconstruction.
SESSION_EVENT_CLASSES = MappingProxyType(
    {
        SESSION_META: SessionMetaEvent,
        MESSAGE: MessageEvent,
        CONTEXT_COMPACTED: ContextCompactedFact,
        HISTORY_EDITED: HistoryEditedFact,
        TURN_CONTEXT: TurnContextEvent,
        PROMPT_REJECTED: PromptRejectedEvent,
        META_UPDATE: MetaUpdateEvent,
        FILE_HISTORY_IMPORTED: FileHistoryImportedEvent,
        FILE_EDIT_PLAN_STORED: FileEditPlanStoredEvent,
        CHECKPOINT: CheckpointEvent,
        LLM_CALL: LLMCallEvent,
        ROUTING_DECISION_FACT: RoutingDecisionFact,
        RUNTIME_CHECKPOINT: RuntimeCheckpointEvent,
        RUNTIME_COMMIT: RuntimeCommitEvent,
        RUNTIME_PROJECTION_ACKNOWLEDGED: RuntimeProjectionAcknowledgedEvent,
        RUNTIME_OPERATION_PREPARED: RuntimeOperationPreparedEvent,
        RUNTIME_OPERATION_COMPLETED: RuntimeOperationCompletedEvent,
        RUNTIME_OPERATION_ABORTED: RuntimeOperationAbortedEvent,
        RUNTIME_HANDOFF_PREPARED: RuntimeHandoffPreparedEvent,
        RUNTIME_HANDOFF_ACTIVATED: RuntimeHandoffActivatedEvent,
        RUNTIME_HANDOFF_RESOLVED: RuntimeHandoffResolvedEvent,
        OUTPUT_CANDIDATE_RECEIVED: OutputCandidateReceivedEvent,
        OUTPUT_VALIDATION_REJECTED: OutputValidationRejectedEvent,
        OUTPUT_ACCEPTED: OutputAcceptedEvent,
        OUTPUT_COMMIT_STARTED: OutputCommitStartedEvent,
        OUTPUT_MIGRATED: OutputMigratedEvent,
        OUTPUT_COMMITTED: OutputCommittedEvent,
        OUTPUT_PUBLICATION_QUEUED: OutputPublicationQueuedEvent,
        OUTPUT_PUBLISHED: OutputPublishedEvent,
        FILE_TRANSACTION_PREPARED: FileTransactionPreparedEvent,
        FILE_TRANSACTION_COMMITTED: FileTransactionCommittedEvent,
        FILE_TRANSACTION_ABORTED: FileTransactionAbortedEvent,
        FILE_TRANSACTION_IN_DOUBT: FileTransactionInDoubtEvent,
        HUNK_DETECTED: HunkDetectedEvent,
        HUNK_REVIEW_TRANSITIONED: HunkReviewTransitionedEvent,
        REWIND_PREPARED: RewindPreparedEvent,
        REWIND_COMMITTED: RewindCommittedEvent,
        REWIND_ABORTED: RewindAbortedEvent,
        REWIND_IN_DOUBT: RewindInDoubtEvent,
    }
)


__all__ = [
    "SCHEMA_VERSION",
    "SESSION_META",
    "MESSAGE",
    "CONTEXT_COMPACTED",
    "HISTORY_EDITED",
    "TURN_CONTEXT",
    "PROMPT_REJECTED",
    "META_UPDATE",
    "FILE_HISTORY_IMPORTED",
    "FILE_EDIT_PLAN_STORED",
    "CHECKPOINT",
    "LLM_CALL",
    "ROUTING_DECISION_FACT",
    "RUNTIME_CHECKPOINT",
    "RUNTIME_COMMIT",
    "RUNTIME_PROJECTION_ACKNOWLEDGED",
    "RUNTIME_OPERATION_PREPARED",
    "RUNTIME_OPERATION_COMPLETED",
    "RUNTIME_OPERATION_ABORTED",
    "RUNTIME_HANDOFF_PREPARED",
    "RUNTIME_HANDOFF_ACTIVATED",
    "RUNTIME_HANDOFF_RESOLVED",
    "OUTPUT_CANDIDATE_RECEIVED",
    "OUTPUT_VALIDATION_REJECTED",
    "OUTPUT_ACCEPTED",
    "OUTPUT_COMMIT_STARTED",
    "OUTPUT_MIGRATED",
    "OUTPUT_COMMITTED",
    "OUTPUT_PUBLICATION_QUEUED",
    "OUTPUT_PUBLISHED",
    "FILE_TRANSACTION_PREPARED",
    "FILE_TRANSACTION_COMMITTED",
    "FILE_TRANSACTION_ABORTED",
    "FILE_TRANSACTION_IN_DOUBT",
    "HUNK_DETECTED",
    "HUNK_REVIEW_TRANSITIONED",
    "REWIND_PREPARED",
    "REWIND_COMMITTED",
    "REWIND_ABORTED",
    "REWIND_IN_DOUBT",
    "SessionMetaEvent",
    "MessageEvent",
    "ContextCompactedFact",
    "HistoryEditedFact",
    "TurnContextEvent",
    "PromptRejectedEvent",
    "MetaUpdateEvent",
    "FileHistoryImportedEvent",
    "FileEditPlanStoredEvent",
    "CheckpointEvent",
    "LLMCallEvent",
    "RoutingDecisionFact",
    "RuntimeCheckpointEvent",
    "RuntimeCommitEvent",
    "RuntimeProjectionAcknowledgedEvent",
    "RuntimeHandoffPreparedEvent",
    "RuntimeHandoffActivatedEvent",
    "RuntimeHandoffResolvedEvent",
    "RuntimeOperationPreparedEvent",
    "RuntimeOperationCompletedEvent",
    "RuntimeOperationAbortedEvent",
    "OutputCandidateReceivedEvent",
    "OutputValidationRejectedEvent",
    "OutputAcceptedEvent",
    "OutputCommitStartedEvent",
    "OutputMigratedEvent",
    "OutputCommittedEvent",
    "OutputPublicationQueuedEvent",
    "OutputPublishedEvent",
    "FileTransactionPreparedEvent",
    "FileTransactionCommittedEvent",
    "FileTransactionAbortedEvent",
    "FileTransactionInDoubtEvent",
    "HunkDetectedEvent",
    "HunkReviewTransitionedEvent",
    "RewindPreparedEvent",
    "RewindCommittedEvent",
    "RewindAbortedEvent",
    "RewindInDoubtEvent",
    "SessionEvent",
    "SESSION_EVENT_CLASSES",
]

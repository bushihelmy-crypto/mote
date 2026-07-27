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
from typing import Any, Dict, List, Optional, Union

from mote.contracts.events.types import (
    OUTPUT_ACCEPTED,
    OUTPUT_CANDIDATE_RECEIVED,
    OUTPUT_COMMIT_STARTED,
    OUTPUT_COMMITTED,
    OUTPUT_MIGRATED,
    OUTPUT_PUBLICATION_QUEUED,
    OUTPUT_PUBLISHED,
    OUTPUT_VALIDATION_REJECTED,
    PROMPT_REJECTED,
    ROUTING_DECISION,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    PromptRejectedEvent,
)
from mote.contracts.fileops.events import (
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
from mote.contracts.handoff import RuntimeHandoffIntent, RuntimeHandoffResolution
from mote.contracts.models.failover import ModelCallSummary
from mote.contracts.runtimes import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
)
from mote.contracts.schema import Message
from mote.contracts.tools import ToolsetManifest, parse_toolset_manifest


def _dataclass_kwargs(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the keys of ``payload`` that name a field of ``cls``.

    Lets a field-shaped event reconstruct via ``cls(**_dataclass_kwargs(...))``
    while tolerating unknown/extra keys (forward-compatible reads).
    """
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in names}


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


def _payload_to_message(payload: Dict[str, Any]) -> Optional[Message]:
    """Reconstruct a Message from a payload dict (forgiving: None on failure)."""
    try:
        return Message.from_dict(payload)
    except Exception:  # noqa: BLE001 — one bad payload must not abort a replay
        return None


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
TERMINAL_STATE = "terminal_state"
KERNEL_STATE = "kernel_state"
BROWSER_STATE = "browser_state"
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
    schema_version: int = SCHEMA_VERSION
    parent_session_id: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    working_dir: str = ""
    original_working_dir: str = ""
    project_root: str = ""
    model: Optional[str] = None
    role_class: Optional[str] = None
    toolset_manifest: Optional[ToolsetManifest] = None

    type = SESSION_META

    def payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        if self.toolset_manifest is not None:
            payload["toolset_manifest"] = [identity.to_payload() for identity in self.toolset_manifest]
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionMetaEvent":
        values = _dataclass_kwargs(cls, payload)
        manifest = values.get("toolset_manifest")
        if manifest is not None:
            values["toolset_manifest"] = parse_toolset_manifest(manifest)
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
        # ``message`` is None when the payload fails to reconstruct; callers
        # treat a None message as a skipped (unloadable) record.
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
        messages = [
            message
            for message in (_payload_to_message(item) for item in payload.get("model_context", []))
            if message is not None
        ]
        return cls(
            model_context_messages=messages,
            source_message_ids=[str(message_id) for message_id in payload.get("source_message_ids", []) if message_id],
            summary=str(payload.get("summary", "")),
            strategy=str(payload.get("strategy", "")),
            trigger=str(payload.get("trigger", "auto")),
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
        return cls(
            removed_message_ids=[
                str(message_id) for message_id in payload.get("removed_message_ids", []) if message_id
            ],
            clear_all=payload.get("clear_all") is True,
            reason=str(payload.get("reason", "delete")),
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
        }
        if self.summary is not None:
            payload["summary"] = self.summary.model_dump(mode="json")
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

    type = ROUTING_DECISION_FACT

    def payload(self) -> Dict[str, Any]:
        return {"decision": dict(self.decision), "state": dict(self.state)}

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RoutingDecisionFact":
        return cls(
            decision=dict(payload.get("decision", {})),
            state=dict(payload.get("state", {})),
        )


@dataclass
class TerminalStateEvent:
    """The persistent terminal's final environment state (resume restore point).

    Captures the live PTY shell's cwd plus the env diff relative to the shell's
    launch baseline (``env`` = added/changed vars, ``unset`` = vars present at
    launch but removed since). On resume, the latest such event lets the freshly
    started shell be re-seeded (``cd`` + ``export`` + ``unset``) to the saved
    state — *without* re-running any of the original user commands. Last-write-
    wins: replay keeps only the most recent one.
    """

    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    unset: List[str] = field(default_factory=list)
    tool: str = ""

    type = TERMINAL_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "cwd": self.cwd,
            "env": self.env,
            "unset": self.unset,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TerminalStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class KernelStateEvent:
    """The persistent kernel's final environment state (resume restore point).

    The Python sibling of :class:`TerminalStateEvent`. Captures the live Jupyter
    kernel process's cwd plus the env diff relative to the kernel's launch
    baseline (``env`` = added/changed vars, ``unset`` = vars present at launch
    but removed since). On resume, the latest such event lets the freshly started
    kernel be re-seeded (``os.chdir`` + ``os.environ.update`` + ``pop``) to the
    saved state — *without* re-running any of the original user code. Only cwd +
    env are restored; the Python namespace (variables/imports/functions) is not.
    Last-write-wins: replay keeps only the most recent one.

    Tracked as a distinct event from :class:`TerminalStateEvent` (not a shared
    ``tool``-tagged record) so the kernel and terminal restores stay independent
    on replay and never clobber each other.
    """

    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    unset: List[str] = field(default_factory=list)
    tool: str = ""

    type = KERNEL_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "cwd": self.cwd,
            "env": self.env,
            "unset": self.unset,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "KernelStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class BrowserStateEvent:
    """The persistent browser's final browsing state (resume restore point).

    Captures the live Playwright session's open-tab URLs (in page order, plus
    the active tab index) and an optional ``storage_state`` dict ({cookies,
    origins}) carrying the logged-in session. On resume, the latest such event
    lets a freshly launched browser re-open the same tabs (re-navigate to each
    URL) seeded with the saved cookies / localStorage — *without* re-running any
    of the original navigation/click actions. Only the page URLs + storage are
    restored; live DOM state, scroll position, and in-flight JS are not. Last-
    write-wins: replay keeps only the most recent one.

    Tracked as a distinct event from the terminal/kernel state events (not a
    shared ``tool``-tagged record) so the browser restore stays independent on
    replay and never clobbers the others (a session may run a shell + kernel +
    browser, each restored separately).

    Privacy note: ``storage_state`` may include sensitive cookies. New logs use
    managed Runtime checkpoints and keep profile-backed cookies encrypted; this
    event remains readable only for migration of historic rollouts.
    """

    urls: List[str] = field(default_factory=list)
    active: int = 0
    storage_state: Optional[Dict[str, Any]] = None
    tool: str = ""

    type = BROWSER_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "urls": self.urls,
            "active": self.active,
            "storage_state": self.storage_state,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "BrowserStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


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
        return cls(
            checkpoint=RuntimeCheckpoint(
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                epoch=int(payload["epoch"]),
                revision=int(payload["revision"]),
                codec=str(payload["codec"]),
                schema_version=int(payload["schema_version"]),
                payload_ref=str(payload["payload_ref"]),
                alias=str(payload.get("alias", "default")),
                digest=str(payload.get("digest", "")),
                sensitivity=str(payload.get("sensitivity", "private")),
                fidelity=CheckpointFidelity(payload.get("fidelity", "none")),
            ),
            reason=str(payload.get("reason", "")),
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
        checkpoint = payload["checkpoint"]
        return cls(
            fact=RuntimeCommitFact(
                commit_id=str(payload["commit_id"]),
                checkpoint=RuntimeCheckpoint(
                    runtime_id=str(checkpoint["runtime_id"]),
                    kind=str(checkpoint["kind"]),
                    epoch=int(checkpoint["epoch"]),
                    revision=int(checkpoint["revision"]),
                    codec=str(checkpoint["codec"]),
                    schema_version=int(checkpoint["schema_version"]),
                    payload_ref=str(checkpoint["payload_ref"]),
                    alias=str(checkpoint.get("alias", "default")),
                    digest=str(checkpoint.get("digest", "")),
                    sensitivity=str(checkpoint.get("sensitivity", "private")),
                    fidelity=CheckpointFidelity(checkpoint.get("fidelity", "none")),
                ),
                projections=tuple(
                    RuntimeProjectionIntent(
                        intent_id=str(item["intent_id"]),
                        projector=str(item["projector"]),
                        schema_version=int(item["schema_version"]),
                        options=tuple((str(option[0]), str(option[1])) for option in item.get("options", ())),
                    )
                    for item in payload.get("projections", ())
                ),
                reason=str(payload.get("reason", "")),
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
        return cls(
            ack=RuntimeProjectionAck(
                commit_id=str(payload["commit_id"]),
                intent_id=str(payload["intent_id"]),
                status=str(payload.get("status", "completed")),
                error=str(payload.get("error", "")),
                attempts=int(payload.get("attempts", 0)),
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
        checkpoint = payload["base_checkpoint"]
        return cls(
            intent=RuntimeOperationIntent(
                operation_id=str(payload["operation_id"]),
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                alias=str(payload.get("alias", "default")),
                epoch=int(payload["epoch"]),
                base_revision=int(payload["base_revision"]),
                target_revision=int(payload["target_revision"]),
                codec=str(payload["codec"]),
                schema_version=int(payload["schema_version"]),
                payload=str(payload["operation_payload"]),
                base_checkpoint=RuntimeCheckpoint(
                    runtime_id=str(checkpoint["runtime_id"]),
                    kind=str(checkpoint["kind"]),
                    epoch=int(checkpoint["epoch"]),
                    revision=int(checkpoint["revision"]),
                    codec=str(checkpoint["codec"]),
                    schema_version=int(checkpoint["schema_version"]),
                    payload_ref=str(checkpoint["payload_ref"]),
                    alias=str(checkpoint.get("alias", "default")),
                    digest=str(checkpoint.get("digest", "")),
                    sensitivity=str(checkpoint.get("sensitivity", "private")),
                    fidelity=CheckpointFidelity(checkpoint.get("fidelity", "none")),
                ),
                projections=tuple(
                    RuntimeProjectionIntent(
                        intent_id=str(item["intent_id"]),
                        projector=str(item["projector"]),
                        schema_version=int(item["schema_version"]),
                        options=tuple((str(option[0]), str(option[1])) for option in item.get("options", ())),
                    )
                    for item in payload.get("projections", ())
                ),
            )
        )


@dataclass
class RuntimeOperationCompletedEvent:
    operation_id: str
    receipt: RuntimeOperationReceipt | None = None

    type = RUNTIME_OPERATION_COMPLETED

    def payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"operation_id": self.operation_id}
        if self.receipt is not None:
            payload["receipt"] = asdict(self.receipt)
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeOperationCompletedEvent":
        raw_receipt = payload.get("receipt")
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
        checkpoint_payload = payload.get("base_checkpoint")
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
                alias=str(payload.get("alias", "default")),
                epoch=int(payload["epoch"]),
                base_revision=int(payload["base_revision"]),
                target_revision=int(payload["target_revision"]),
                owner_id=str(payload["owner_id"]),
                fencing_token=int(payload["fencing_token"]),
                mode=str(payload["mode"]),
                message=str(payload.get("message", "")),
                selection=tuple(str(item) for item in payload.get("selection", ())),
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
        checkpoint_payload = payload.get("checkpoint")
        return cls(
            resolution=RuntimeHandoffResolution(
                handoff_id=str(payload["handoff_id"]),
                status=str(payload["status"]),
                runtime_id=str(payload["runtime_id"]),
                kind=str(payload["kind"]),
                alias=str(payload.get("alias", "default")),
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
    TerminalStateEvent,
    KernelStateEvent,
    BrowserStateEvent,
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
SESSION_EVENT_CLASSES = {
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
    TERMINAL_STATE: TerminalStateEvent,
    KERNEL_STATE: KernelStateEvent,
    BROWSER_STATE: BrowserStateEvent,
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
    "TERMINAL_STATE",
    "KERNEL_STATE",
    "BROWSER_STATE",
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
    "TerminalStateEvent",
    "KernelStateEvent",
    "BrowserStateEvent",
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

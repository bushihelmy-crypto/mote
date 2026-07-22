"""Session replay — reconstruct stored history from a rollout log (Phase 2).

The append-only rollout is the truth source; resume rebuilds the in-memory
history from it. A single forward pass suffices because the log is ordered and a
``compacted`` event already carries the *full* post-compaction history
(``replacement_history``):

  * ``message``    -> append the message to the running history
  * ``compacted``  -> RESET the running history to ``replacement_history``
                      (later ``message`` events then append after it)
  * ``session_meta`` -> capture the session identity/cwd
  * ``turn_context`` / ``meta_update`` -> ignored for history rebuild

So the final history equals ``latest_checkpoint + messages_after_it`` — the same
state that was live when the session stopped. Pre-checkpoint message appends are
harmlessly discarded by the reset, mirroring how the live ContextManager swapped
its list on compaction. This avoids the reverse-scan Codex needs by relying on
the checkpoint being self-contained.

Reconstruction is forgiving: a message payload that fails to load is skipped
(``Message.load`` returns None) so one bad line never aborts the whole replay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from mote.common.logs import log_call
from mote.common.schema import Message
from mote.session.events import (
    BrowserStateEvent,
    CompactedEvent,
    KernelStateEvent,
    MessageEvent,
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    OutputValidationRejectedEvent,
    SessionMetaEvent,
    TerminalStateEvent,
    parse_event,
)
from mote.session.log import SessionLog


@dataclass
class ReplayResult:
    """The outcome of replaying a rollout log into a history."""

    messages: List[Message] = field(default_factory=list)
    meta: Optional[Dict] = None
    message_events: int = 0
    checkpoints: int = 0
    skipped: int = 0
    #: Latest persisted persistent-terminal state ({cwd, env, unset}), or None.
    #: Used by resume to re-seed a fresh shell without re-running user commands.
    terminal_state: Optional[Dict] = None
    #: Latest persisted persistent-kernel state ({cwd, env, unset}), or None.
    #: Used by resume to re-seed a fresh kernel without re-running user code.
    #: Independent of ``terminal_state`` (separate event stream, no clobber).
    kernel_state: Optional[Dict] = None
    #: Latest persisted persistent-browser state ({urls, active, storage_state}),
    #: or None. Used by resume to re-open the same tabs (seeded with the saved
    #: session) without re-running navigation/click actions. Independent of the
    #: terminal/kernel state above (separate event stream, no clobber).
    browser_state: Optional[Dict] = None
    output_state: Optional[Dict] = None
    output_states: Dict[str, Dict] = field(default_factory=dict)

    @property
    def from_checkpoint(self) -> bool:
        return self.checkpoints > 0


@log_call(level="DEBUG")
def replay(log: SessionLog) -> ReplayResult:
    """Reconstruct the stored history from ``log`` (single forward pass)."""
    result = ReplayResult()
    for record in log.iter_raw():
        event = parse_event(record)
        if (
            isinstance(
                event,
                (
                    OutputCandidateReceivedEvent,
                    OutputValidationRejectedEvent,
                    OutputAcceptedEvent,
                    OutputCommitStartedEvent,
                    OutputMigratedEvent,
                    OutputCommittedEvent,
                    OutputPublicationQueuedEvent,
                    OutputPublishedEvent,
                ),
            )
            and event.run_id
        ):
            result.output_state = dict(result.output_states.get(event.run_id, {}))
        if isinstance(event, SessionMetaEvent):
            result.meta = asdict(event)
        elif isinstance(event, MessageEvent):
            result.message_events += 1
            if event.message is None:
                result.skipped += 1  # unloadable payload, skipped (counted)
            else:
                result.messages.append(event.message)
        elif isinstance(event, CompactedEvent):
            result.checkpoints += 1
            # Reset to the self-contained checkpoint history.
            result.messages = list(event.messages)
        elif isinstance(event, TerminalStateEvent):
            # Last-write-wins: only the most recent terminal state is restored
            # (not part of the message-history rebuild).
            result.terminal_state = {
                "cwd": event.cwd,
                "env": dict(event.env),
                "unset": list(event.unset),
            }
        elif isinstance(event, KernelStateEvent):
            # Last-write-wins: only the most recent kernel state is restored
            # (not part of the message-history rebuild). Independent of the
            # terminal state above — both restore on resume without clobbering.
            result.kernel_state = {
                "cwd": event.cwd,
                "env": dict(event.env),
                "unset": list(event.unset),
            }
        elif isinstance(event, BrowserStateEvent):
            # Last-write-wins: only the most recent browser state is restored
            # (not part of the message-history rebuild). Independent of the
            # terminal/kernel state above — restores on resume without clobbering.
            result.browser_state = {
                "urls": list(event.urls),
                "active": event.active,
                "storage_state": event.storage_state,
            }
        elif isinstance(event, OutputCandidateReceivedEvent):
            result.output_state = {
                "status": "candidate_received",
                "candidate_id": event.candidate_id,
                "contract_id": event.contract_id,
                "schema_fingerprint": event.schema_fingerprint,
                "representation": event.representation,
                "raw": event.raw,
            }
            if event.run_id:
                result.output_state["run_id"] = event.run_id
        elif isinstance(event, OutputValidationRejectedEvent):
            state = dict(result.output_state or {})
            state.update(
                status=("awaiting_correction" if event.correction_allowed else "correction_exhausted"),
                candidate_id=event.candidate_id,
                contract_id=event.contract_id,
                issues=list(event.issues),
                correction_attempts=event.correction_attempt,
                corrections_remaining=event.corrections_remaining,
            )
            if event.validator_provenance:
                state["validator_provenance"] = list(event.validator_provenance)
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputAcceptedEvent):
            state = dict(result.output_state or {})
            state.update(
                {
                    "status": "accepted",
                    "candidate_id": event.candidate_id,
                    "contract_id": event.contract_id,
                    "schema_fingerprint": event.schema_fingerprint,
                    "value": event.value,
                    "correction_attempts": event.correction_attempts,
                }
            )
            if event.validator_provenance:
                state["validator_provenance"] = list(event.validator_provenance)
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputCommitStartedEvent):
            state = dict(result.output_state or {})
            state.update(
                status="commit_started",
                candidate_id=event.candidate_id,
                contract_id=event.contract_id,
                fencing_token=event.fencing_token,
            )
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputMigratedEvent):
            state = {
                "status": "accepted",
                "candidate_id": event.candidate_id,
                "contract_id": event.target_contract_id,
                "schema_fingerprint": event.target_schema_fingerprint,
                "value": event.value,
                "correction_attempts": 0,
                "migration_provenance": list(event.steps),
            }
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputCommittedEvent):
            prior_migration = (result.output_state or {}).get("migration_provenance")
            state = {
                "status": "committed",
                "candidate_id": event.candidate_id,
                "contract_id": event.contract_id,
                "schema_fingerprint": event.schema_fingerprint,
                "value": event.value,
                "correction_attempts": event.correction_attempts,
                "fencing_token": event.fencing_token,
            }
            if prior_migration:
                state["migration_provenance"] = prior_migration
            if event.validator_provenance:
                state["validator_provenance"] = list(event.validator_provenance)
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputPublicationQueuedEvent):
            state = dict(result.output_state or {})
            state.update(
                status="publication_queued",
                publication_id=event.publication_id,
                candidate_id=event.candidate_id,
                contract_id=event.contract_id,
            )
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        elif isinstance(event, OutputPublishedEvent):
            state = dict(result.output_state or {})
            state.update(
                status="published",
                candidate_id=event.candidate_id,
                contract_id=event.contract_id,
            )
            if event.publication_id:
                state["publication_id"] = event.publication_id
            if event.run_id:
                state["run_id"] = event.run_id
            result.output_state = state
        if result.output_state and result.output_state.get("run_id"):
            result.output_state["run_kind"] = getattr(event, "run_kind", "agent")
            result.output_states[result.output_state["run_id"]] = dict(result.output_state)
        # turn_context / meta_update / unknown: not part of the history rebuild.
    return result


__all__ = ["ReplayResult", "replay"]

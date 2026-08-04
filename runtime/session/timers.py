"""Canonical process-independent Session timer facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerCommitReceipt
from mote.runtime.session.run_domain_activation import require_run_domain_activation
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace

SESSION_TIMER_SCHEMA = "mote.session-timer/v1"


class SessionTimerState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    MISFIRED = "misfired"


class SessionTimerIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionTimerRecord:
    timer_id: str
    deadline: AbsoluteInstant
    state: SessionTimerState

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SESSION_TIMER_SCHEMA,
                "timer_id": self.timer_id,
                "deadline": self.deadline.to_dict(),
                "state": self.state.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "SessionTimerRecord":
        if set(raw) != {"schema", "timer_id", "deadline", "state"} or raw.get("schema") != SESSION_TIMER_SCHEMA:
            raise SessionTimerIntegrityError("Session timer schema or fields are invalid")
        timer_id = raw["timer_id"]
        if type(timer_id) is not str or not timer_id:
            raise SessionTimerIntegrityError("Session timer identity is invalid")
        try:
            deadline = AbsoluteInstant.from_dict(raw["deadline"])
            deadline.require_clock(UNIX_UTC_CLOCK)
            state = SessionTimerState(raw["state"])
        except (TypeError, ValueError) as exc:
            raise SessionTimerIntegrityError("Session timer deadline or state is invalid") from exc
        return cls(timer_id, deadline, state)


class SessionTimerStore(AppendOnlyLedger[SessionTimerRecord]):
    def __init__(self, session_id: str, workspace: SessionWorkspace) -> None:
        path = workspace.space(session_id, SessionSpace.LEDGER) / "session-timers.jsonl"
        require_run_domain_activation(path.parent)
        super().__init__(path)

    def _parse_record(self, data: dict[str, object]) -> SessionTimerRecord:
        return SessionTimerRecord.from_dict(data)

    def _record_key(self, record: SessionTimerRecord) -> str:
        return record.timer_id

    def _validate_transition(self, previous: SessionTimerRecord | None, record: SessionTimerRecord) -> None:
        if previous is None:
            if record.state is not SessionTimerState.PENDING:
                raise SessionTimerIntegrityError("Session timer must begin pending")
            return
        if previous.state is not SessionTimerState.PENDING or record.state is SessionTimerState.PENDING:
            raise SessionTimerIntegrityError("Session timer lifecycle is terminal and monotonic")
        if previous.deadline != record.deadline:
            raise SessionTimerIntegrityError("Session timer deadline changed during settlement")

    def schedule(self, duration_seconds: float) -> SessionTimerRecord:
        if type(duration_seconds) not in {int, float} or duration_seconds <= 0:
            raise ValueError("Session timer duration must be positive")
        deadline = AbsoluteInstant.from_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=float(duration_seconds))
        )
        record = SessionTimerRecord(f"timer-{uuid4().hex}", deadline, SessionTimerState.PENDING)
        self.append(record)
        return record

    def pending(self) -> tuple[SessionTimerRecord, ...]:
        return tuple(record for record in self.records() if record.state is SessionTimerState.PENDING)

    def settle(self, timer_id: str, state: SessionTimerState) -> LedgerCommitReceipt:
        if state is SessionTimerState.PENDING:
            raise ValueError("Session timer settlement must be terminal")
        prior = self.get(timer_id)
        if prior is None:
            raise SessionTimerIntegrityError("Session timer settlement has no pending fact")
        return self.append(SessionTimerRecord(timer_id, prior.deadline, state))


__all__ = [
    "SESSION_TIMER_SCHEMA",
    "SessionTimerIntegrityError",
    "SessionTimerRecord",
    "SessionTimerState",
    "SessionTimerStore",
]

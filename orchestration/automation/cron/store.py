"""Versioned transactional owner for Cron schedules and fire occurrences."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Optional

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.lock import SchedulerFence, verify_scheduler_fence
from mote.orchestration.automation.cron.task import CronTask, SessionCronTaskId
from mote.runtime.persistence import atomic_write
from mote.runtime.telemetry.logging import log_class

SCHEDULES_DIRNAME = ".agent_schedules"
SCHEDULES_FILENAME = "scheduled_tasks.json"
_STORE_LOCK_FILENAME = "scheduled_tasks.store.lock"
_SCHEMA = "mote.cron-schedule/v3"
_ENVELOPE_FIELDS = {"schema", "schedule_id", "revision", "tasks", "occurrences"}
_OCCURRENCE_FIELDS = {
    "occurrence_id",
    "task_id",
    "task_revision",
    "scheduled_at",
    "observed_at",
    "state",
    "attempt",
    "receipt_id",
    "reason",
    "next_attempt_at",
    "delete_on_accept",
}
_MAX_ATTEMPTS = 8


class CronStoreError(RuntimeError):
    pass


class CronStoreCorruptionError(CronStoreError):
    pass


class CronTailTornWriteError(CronStoreCorruptionError):
    pass


class CronRevisionConflict(CronStoreError):
    pass


class CronOccurrenceState(StrEnum):
    INTENT_COMMITTED = "intent_committed"
    DISPATCHING = "dispatching"
    DEFERRED = "deferred"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class CronOccurrence:
    occurrence_id: str
    task_id: str
    task_revision: int
    scheduled_at_ms: int
    observed_at_ms: int
    state: CronOccurrenceState
    attempt: int
    receipt_id: str | None
    reason: str | None
    next_attempt_at_ms: int | None
    delete_on_accept: bool

    def __post_init__(self) -> None:
        expected = f"cron:{self.task_id}:{self.task_revision}:{self.scheduled_at_ms}"
        if self.occurrence_id != expected:
            raise ValueError("cron occurrence identity mismatch")
        if type(self.task_revision) is not int or self.task_revision < 0:
            raise ValueError("cron occurrence task revision is invalid")
        for name in ("scheduled_at_ms", "observed_at_ms", "attempt"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"cron occurrence {name} is invalid")
        if self.observed_at_ms < self.scheduled_at_ms:
            raise ValueError("cron occurrence observation precedes schedule")
        if self.next_attempt_at_ms is not None and (
            type(self.next_attempt_at_ms) is not int or self.next_attempt_at_ms < self.observed_at_ms
        ):
            raise ValueError("cron occurrence next attempt instant is invalid")
        if type(self.delete_on_accept) is not bool:
            raise ValueError("cron occurrence delete disposition must be boolean")
        for name in ("receipt_id", "reason"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"cron occurrence {name} must be non-empty when present")

    @property
    def terminal(self) -> bool:
        return self.state in {
            CronOccurrenceState.ACCEPTED,
            CronOccurrenceState.REJECTED,
            CronOccurrenceState.IN_DOUBT,
        }


@dataclass(frozen=True, slots=True)
class CronScheduleSnapshot:
    schedule_id: str
    revision: int
    tasks: tuple[CronTask, ...]
    occurrences: tuple[CronOccurrence, ...] = ()


def _instant(milliseconds: int) -> dict[str, object]:
    return AbsoluteInstant(1, UNIX_UTC_CLOCK, milliseconds * 1_000_000).to_dict()


def _milliseconds(raw: object, label: str) -> int:
    instant = AbsoluteInstant.from_dict(raw)
    instant.require_clock(UNIX_UTC_CLOCK)
    if instant.epoch_nanoseconds % 1_000_000:
        raise ValueError(f"cron {label} must have millisecond precision")
    return instant.epoch_nanoseconds // 1_000_000


def _occurrence_to_dict(value: CronOccurrence) -> dict[str, object]:
    return {
        "occurrence_id": value.occurrence_id,
        "task_id": value.task_id,
        "task_revision": value.task_revision,
        "scheduled_at": _instant(value.scheduled_at_ms),
        "observed_at": _instant(value.observed_at_ms),
        "state": value.state.value,
        "attempt": value.attempt,
        "receipt_id": value.receipt_id,
        "reason": value.reason,
        "next_attempt_at": (None if value.next_attempt_at_ms is None else _instant(value.next_attempt_at_ms)),
        "delete_on_accept": value.delete_on_accept,
    }


def _occurrence_from_dict(raw: object) -> CronOccurrence:
    if type(raw) is not dict or set(raw) != _OCCURRENCE_FIELDS:
        raise ValueError("cron occurrence fields are not canonical")
    assert isinstance(raw, dict)
    for name in ("occurrence_id", "task_id", "state"):
        if type(raw[name]) is not str:
            raise ValueError(f"cron occurrence {name} primitive is invalid")
    for name in ("task_revision", "attempt"):
        if type(raw[name]) is not int:
            raise ValueError(f"cron occurrence {name} primitive is invalid")
    for name in ("receipt_id", "reason"):
        if raw[name] is not None and type(raw[name]) is not str:
            raise ValueError(f"cron occurrence {name} primitive is invalid")
    if type(raw["delete_on_accept"]) is not bool:
        raise ValueError("cron occurrence delete primitive is invalid")
    return CronOccurrence(
        occurrence_id=raw["occurrence_id"],
        task_id=raw["task_id"],
        task_revision=raw["task_revision"],
        scheduled_at_ms=_milliseconds(raw["scheduled_at"], "scheduled_at"),
        observed_at_ms=_milliseconds(raw["observed_at"], "observed_at"),
        state=CronOccurrenceState(raw["state"]),
        attempt=raw["attempt"],
        receipt_id=raw["receipt_id"],
        reason=raw["reason"],
        next_attempt_at_ms=(
            None if raw["next_attempt_at"] is None else _milliseconds(raw["next_attempt_at"], "next_attempt_at")
        ),
        delete_on_accept=raw["delete_on_accept"],
    )


@log_class(level="DEBUG", exclude={"path"})
class CronTaskStore:
    """Single locked command owner for schedule and occurrence mutations."""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            raise ValueError("CronTaskStore requires an explicit base directory")
        self._dir = Path(base_dir)
        self._path = self._dir / SCHEDULES_FILENAME
        self._lock_path = self._dir / _STORE_LOCK_FILENAME
        canonical = str(self._path.resolve(strict=False)).encode("utf-8")
        self._schedule_id = hashlib.sha256(b"mote.cron-schedule\0" + canonical).hexdigest()
        self._session: dict[str, CronTask] = {}

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _command_lock(self) -> Iterator[None]:
        self._dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load_snapshot(self) -> CronScheduleSnapshot:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CronScheduleSnapshot(self._schedule_id, 0, ())
        except (OSError, UnicodeError) as error:
            raise CronStoreError("cron schedule cannot be read") from error
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            self._quarantine(raw)
            if error.pos >= max(0, len(raw.rstrip()) - 1):
                raise CronTailTornWriteError("cron schedule has a torn tail") from error
            raise CronStoreCorruptionError("cron schedule JSON is corrupt") from error
        try:
            return self._decode(parsed)
        except (TypeError, ValueError, CronStoreCorruptionError) as error:
            self._quarantine(raw)
            if isinstance(error, CronStoreCorruptionError):
                raise
            raise CronStoreCorruptionError("cron schedule contract is corrupt") from error

    def load(self) -> list[CronTask]:
        return list(self.load_snapshot().tasks)

    def decode_candidate(self, parsed: object) -> CronScheduleSnapshot:
        """Decode a migration candidate through the canonical Cron owner."""
        return self._decode(parsed)

    def _decode(self, parsed: object) -> CronScheduleSnapshot:
        if type(parsed) is not dict:
            raise CronStoreCorruptionError("cron schedule envelope must be an object")
        schema = parsed.get("schema")
        if schema != _SCHEMA or set(parsed) != _ENVELOPE_FIELDS:
            raise CronStoreCorruptionError("unsupported or malformed cron schedule schema")
        base = self._decode_header(parsed)
        if type(parsed["occurrences"]) is not list:
            raise CronStoreCorruptionError("cron occurrences must be a list")
        occurrences = tuple(_occurrence_from_dict(item) for item in parsed["occurrences"])
        self._validate_occurrences(base.tasks, occurrences)
        return CronScheduleSnapshot(base.schedule_id, base.revision, base.tasks, occurrences)

    def _decode_header(
        self,
        parsed: dict[object, object],
    ) -> CronScheduleSnapshot:
        if type(parsed["schedule_id"]) is not str or parsed["schedule_id"] != self._schedule_id:
            raise CronStoreCorruptionError("cron schedule identity mismatch")
        if type(parsed["revision"]) is not int or parsed["revision"] < 0:
            raise CronStoreCorruptionError("cron schedule revision is invalid")
        if type(parsed["tasks"]) is not list:
            raise CronStoreCorruptionError("cron schedule tasks must be a list")
        tasks = tuple(CronTask.from_dict(item) for item in parsed["tasks"])
        self._validate_tasks(tasks)
        return CronScheduleSnapshot(self._schedule_id, parsed["revision"], tasks)

    @staticmethod
    def _validate_tasks(tasks: tuple[CronTask, ...]) -> None:
        ids = [str(task.id) for task in tasks]
        if len(ids) != len(set(ids)):
            raise CronStoreCorruptionError("cron schedule contains duplicate task ids")

    @staticmethod
    def _validate_occurrences(tasks: tuple[CronTask, ...], occurrences: tuple[CronOccurrence, ...]) -> None:
        ids = [item.occurrence_id for item in occurrences]
        if len(ids) != len(set(ids)):
            raise CronStoreCorruptionError("cron schedule contains duplicate occurrences")
        task_revisions = {(str(task.id), task.revision) for task in tasks}
        for item in occurrences:
            if (
                not item.terminal
                and (
                    item.task_id,
                    item.task_revision,
                )
                not in task_revisions
            ):
                raise CronStoreCorruptionError("active cron occurrence task revision mismatch")

    def _quarantine(self, raw: str) -> None:
        digest = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()
        path = self._dir / f"{SCHEDULES_FILENAME}.quarantine-{digest}.json"
        if not path.exists():
            atomic_write(path, raw.encode("utf-8", errors="surrogatepass"))

    def _commit(
        self,
        tasks: list[CronTask],
        occurrences: list[CronOccurrence],
        *,
        expected_revision: int,
        current: CronScheduleSnapshot,
    ) -> int:
        if current.revision != expected_revision:
            raise CronRevisionConflict(
                f"cron schedule revision changed: expected {expected_revision}, current {current.revision}"
            )
        durable = [task for task in tasks if task.durable]
        self._validate_tasks(tuple(durable))
        self._validate_occurrences(tuple(durable), tuple(occurrences))
        revision = current.revision + 1
        body = {
            "schema": _SCHEMA,
            "schedule_id": self._schedule_id,
            "revision": revision,
            "tasks": [task.to_dict() for task in durable],
            "occurrences": [_occurrence_to_dict(item) for item in occurrences],
        }
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        atomic_write(self._path, text.encode("utf-8"))
        return revision

    def save(self, tasks: list[CronTask], *, expected_revision: int) -> int:
        with self._command_lock():
            current = self.load_snapshot()
            return self._commit(
                tasks,
                list(current.occurrences),
                expected_revision=expected_revision,
                current=current,
            )

    def add(self, task: CronTask, *, capacity_limit: int) -> CronTask:
        if type(capacity_limit) is not int or capacity_limit < 1:
            raise ValueError("cron capacity limit must be a positive integer")
        with self._command_lock():
            current = self.load_snapshot()
            if len(current.tasks) + len(self._session) >= capacity_limit:
                raise ValueError(f"scheduled task limit reached ({capacity_limit})")
            if task.durable:
                if any(existing.id == task.id for existing in current.tasks):
                    raise CronRevisionConflict("cron task identity already exists")
                self._commit(
                    [*current.tasks, task],
                    list(current.occurrences),
                    expected_revision=current.revision,
                    current=current,
                )
            else:
                self._session[str(task.id)] = task
        return task

    def remove(self, ids: list[str]) -> int:
        id_set = set(ids)
        if not id_set:
            return 0
        removed = sum(self._session.pop(task_id, None) is not None for task_id in id_set)
        with self._command_lock():
            current = self.load_snapshot()
            if any(item.task_id in id_set and not item.terminal for item in current.occurrences):
                raise CronRevisionConflict("cron task has an unsettled occurrence")
            remaining = [task for task in current.tasks if task.id not in id_set]
            durable_removed = len(current.tasks) - len(remaining)
            if durable_removed:
                self._commit(
                    remaining,
                    list(current.occurrences),
                    expected_revision=current.revision,
                    current=current,
                )
        return removed + durable_removed

    def claim_occurrence(
        self,
        *,
        fence: SchedulerFence,
        task_id: str,
        expected_task_revision: int,
        scheduled_at_ms: int,
        observed_at_ms: int,
        delete_on_accept: bool,
    ) -> CronOccurrence:
        occurrence_id = f"cron:{task_id}:{expected_task_revision}:{scheduled_at_ms}"
        with self._command_lock():
            verify_scheduler_fence(self._dir, fence)
            current = self.load_snapshot()
            existing = next(
                (item for item in current.occurrences if item.occurrence_id == occurrence_id),
                None,
            )
            if existing is not None:
                return existing
            task = next((item for item in current.tasks if item.id == task_id), None)
            if task is None or task.revision != expected_task_revision:
                raise CronRevisionConflict("cron occurrence task revision changed")
            occurrence = CronOccurrence(
                occurrence_id=occurrence_id,
                task_id=task_id,
                task_revision=expected_task_revision,
                scheduled_at_ms=scheduled_at_ms,
                observed_at_ms=observed_at_ms,
                state=CronOccurrenceState.INTENT_COMMITTED,
                attempt=0,
                receipt_id=None,
                reason=None,
                next_attempt_at_ms=None,
                delete_on_accept=delete_on_accept,
            )
            self._commit(
                list(current.tasks),
                [*current.occurrences, occurrence],
                expected_revision=current.revision,
                current=current,
            )
            return occurrence

    def begin_dispatch(
        self,
        occurrence_id: str,
        *,
        fence: SchedulerFence,
        now_ms: int,
    ) -> CronOccurrence:
        with self._command_lock():
            verify_scheduler_fence(self._dir, fence)
            current = self.load_snapshot()
            occurrences = list(current.occurrences)
            index = next(
                (position for position, item in enumerate(occurrences) if item.occurrence_id == occurrence_id),
                None,
            )
            if index is None:
                raise CronRevisionConflict("cron occurrence does not exist")
            occurrence = occurrences[index]
            if occurrence.state is CronOccurrenceState.DEFERRED:
                if occurrence.next_attempt_at_ms is None or now_ms < occurrence.next_attempt_at_ms:
                    return occurrence
            elif occurrence.state is CronOccurrenceState.DISPATCHING:
                in_doubt = replace(
                    occurrence,
                    state=CronOccurrenceState.IN_DOUBT,
                    reason="scheduler restarted with an unsettled dispatch attempt",
                    next_attempt_at_ms=None,
                )
                occurrences[index] = in_doubt
                self._commit(
                    list(current.tasks),
                    occurrences,
                    expected_revision=current.revision,
                    current=current,
                )
                return in_doubt
            elif occurrence.state is not CronOccurrenceState.INTENT_COMMITTED:
                return occurrence
            if occurrence.attempt >= _MAX_ATTEMPTS:
                rejected = replace(
                    occurrence,
                    state=CronOccurrenceState.REJECTED,
                    reason="cron trigger retry budget exhausted",
                    next_attempt_at_ms=None,
                )
                occurrences[index] = rejected
                self._commit(list(current.tasks), occurrences, expected_revision=current.revision, current=current)
                return rejected
            dispatching = replace(
                occurrence,
                state=CronOccurrenceState.DISPATCHING,
                attempt=occurrence.attempt + 1,
                observed_at_ms=now_ms,
                next_attempt_at_ms=None,
            )
            occurrences[index] = dispatching
            self._commit(list(current.tasks), occurrences, expected_revision=current.revision, current=current)
            return dispatching

    def settle_receipt(
        self,
        occurrence_id: str,
        *,
        fence: SchedulerFence,
        expected_attempt: int,
        receipt: TriggerReceipt,
        settled_at_ms: int,
    ) -> CronOccurrence:
        with self._command_lock():
            verify_scheduler_fence(self._dir, fence)
            current = self.load_snapshot()
            tasks = list(current.tasks)
            occurrences = list(current.occurrences)
            index = next(
                (position for position, item in enumerate(occurrences) if item.occurrence_id == occurrence_id),
                None,
            )
            if index is None:
                raise CronRevisionConflict("cron occurrence does not exist")
            occurrence = occurrences[index]
            if occurrence.state is not CronOccurrenceState.DISPATCHING or occurrence.attempt != expected_attempt:
                raise CronRevisionConflict("cron receipt does not match the active dispatch attempt")
            if receipt.disposition is TriggerDisposition.ACCEPTED:
                if not receipt.receipt_id:
                    raise ValueError("accepted cron trigger requires a receipt identity")
                task_index = next(
                    (
                        position
                        for position, task in enumerate(tasks)
                        if task.id == occurrence.task_id and task.revision == occurrence.task_revision
                    ),
                    None,
                )
                if task_index is None:
                    raise CronRevisionConflict("accepted cron occurrence task revision changed")
                task = tasks[task_index]
                if occurrence.delete_on_accept:
                    tasks.pop(task_index)
                else:
                    tasks[task_index] = replace(
                        task,
                        last_fired_at=settled_at_ms,
                        revision=task.revision + 1,
                    )
                settled = replace(
                    occurrence,
                    state=CronOccurrenceState.ACCEPTED,
                    receipt_id=receipt.receipt_id,
                    reason=None,
                    next_attempt_at_ms=None,
                )
            elif receipt.disposition is TriggerDisposition.DEFERRED:
                delay = min(1_000 * (2 ** max(occurrence.attempt - 1, 0)), 60_000)
                settled = replace(
                    occurrence,
                    state=CronOccurrenceState.DEFERRED,
                    receipt_id=receipt.receipt_id,
                    reason=receipt.reason or "trigger deferred",
                    next_attempt_at_ms=settled_at_ms + delay,
                )
            else:
                settled = replace(
                    occurrence,
                    state=CronOccurrenceState.REJECTED,
                    receipt_id=receipt.receipt_id,
                    reason=receipt.reason or "trigger rejected",
                    next_attempt_at_ms=None,
                )
            occurrences[index] = settled
            self._commit(tasks, occurrences, expected_revision=current.revision, current=current)
            return settled

    def mark_in_doubt(
        self,
        occurrence_id: str,
        *,
        fence: SchedulerFence,
        expected_attempt: int,
        reason: str,
    ) -> CronOccurrence:
        with self._command_lock():
            verify_scheduler_fence(self._dir, fence)
            current = self.load_snapshot()
            occurrences = list(current.occurrences)
            index = next(
                (position for position, item in enumerate(occurrences) if item.occurrence_id == occurrence_id),
                None,
            )
            if index is None:
                raise CronRevisionConflict("cron occurrence does not exist")
            occurrence = occurrences[index]
            if occurrence.state is not CronOccurrenceState.DISPATCHING or occurrence.attempt != expected_attempt:
                raise CronRevisionConflict("in-doubt settlement does not match dispatch attempt")
            settled = replace(
                occurrence,
                state=CronOccurrenceState.IN_DOUBT,
                reason=reason,
                next_attempt_at_ms=None,
            )
            occurrences[index] = settled
            self._commit(list(current.tasks), occurrences, expected_revision=current.revision, current=current)
            return settled

    def session_tasks(self) -> list[CronTask]:
        return list(self._session.values())

    def settle_session_task(
        self, task_id: SessionCronTaskId, *, expected_revision: int, settled_at_ms: int
    ) -> CronTask:
        current = self._session.get(str(task_id))
        if current is None or current.revision != expected_revision:
            raise CronRevisionConflict("session cron task revision changed")
        settled = replace(
            current,
            last_fired_at=settled_at_ms,
            revision=current.revision + 1,
        )
        self._session[str(task_id)] = settled
        return settled

    def remove_session_task(self, task_id: str) -> bool:
        return self._session.pop(task_id, None) is not None

    def list(self) -> list[CronTask]:
        return [*self.load(), *self._session.values()]

    def get(self, task_id: str) -> Optional[CronTask]:
        return next((task for task in self.list() if task.id == task_id), None)


__all__ = [
    "CronOccurrence",
    "CronOccurrenceState",
    "CronRevisionConflict",
    "CronScheduleSnapshot",
    "CronStoreCorruptionError",
    "CronStoreError",
    "CronTailTornWriteError",
    "CronTaskStore",
    "SCHEDULES_DIRNAME",
    "SCHEDULES_FILENAME",
]

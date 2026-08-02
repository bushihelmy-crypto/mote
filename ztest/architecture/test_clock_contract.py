from __future__ import annotations

import pickle
from datetime import datetime
from pathlib import Path

import pytest

from mote.contracts.clock import (
    UNIX_UTC_CLOCK,
    AbsoluteInstant,
    ClockIdentity,
    LocalTimeDisposition,
    MonotonicMark,
    resolve_local_datetime,
)
from mote.contracts.ports.clock import ClockSource
from mote.product.composition.clock import build_clock_source

ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, epoch_nanoseconds: int, process_id: str) -> None:
        self.epoch_nanoseconds = epoch_nanoseconds
        self.tick_nanoseconds = 0
        self.process_id = process_id

    @property
    def durable_clock_identity(self) -> ClockIdentity:
        return UNIX_UTC_CLOCK

    def now(self) -> AbsoluteInstant:
        return AbsoluteInstant(1, UNIX_UTC_CLOCK, self.epoch_nanoseconds)

    def monotonic_mark(self) -> MonotonicMark:
        return MonotonicMark(self.process_id, self.tick_nanoseconds)


def test_absolute_instant_strict_round_trip_and_clock_identity() -> None:
    instant = AbsoluteInstant.from_datetime(datetime.fromisoformat("2026-08-01T12:34:56.123456+08:00"))

    restored = AbsoluteInstant.from_dict(instant.to_dict())

    assert restored == instant
    assert restored.to_datetime(expected_clock=UNIX_UTC_CLOCK).isoformat() == ("2026-08-01T04:34:56.123456+00:00")
    with pytest.raises(ValueError, match="clock identity mismatch"):
        restored.to_datetime(expected_clock=ClockIdentity(1, "another-clock"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema="mote.absolute-instant/v2"),
        lambda value: value.update(extra=True),
        lambda value: value.pop("clock"),
        lambda value: value.update(epoch_nanoseconds=True),
        lambda value: value["clock"].update(value=1),
    ],
)
def test_absolute_instant_invalid_shape_fails_closed(mutation) -> None:
    raw = AbsoluteInstant(1, UNIX_UTC_CLOCK, 10).to_dict()
    mutation(raw)

    with pytest.raises(ValueError):
        AbsoluteInstant.from_dict(raw)


def test_clock_schema_and_nested_identity_reject_boolean_or_wrong_carrier() -> None:
    with pytest.raises(ValueError, match="schema"):
        ClockIdentity(True, "unix-utc")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="schema"):
        AbsoluteInstant(True, UNIX_UTC_CLOCK, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ClockIdentity"):
        AbsoluteInstant(1, object(), 1)  # type: ignore[arg-type]
    raw = AbsoluteInstant(1, UNIX_UTC_CLOCK, 1).to_dict()
    raw["clock"]["schema_version"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        AbsoluteInstant.from_dict(raw)


def test_naive_datetime_is_never_a_durable_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AbsoluteInstant.from_datetime(datetime(2026, 8, 1, 12, 0))


def test_restart_preserves_deadline_while_monotonic_marks_do_not_cross_process() -> None:
    before_restart = FakeClock(1_000, "process-a")
    deadline = AbsoluteInstant(1, UNIX_UTC_CLOCK, 2_000)
    start = before_restart.monotonic_mark()
    before_restart.tick_nanoseconds = 500
    assert before_restart.monotonic_mark().elapsed_nanoseconds(start) == 500

    after_restart = FakeClock(1_500, "process-b")
    assert after_restart.now().is_at_or_after(deadline) is False
    with pytest.raises(ValueError, match="different process"):
        after_restart.monotonic_mark().elapsed_nanoseconds(start)

    after_restart.epoch_nanoseconds = 2_001
    assert after_restart.now().is_at_or_after(deadline) is True


def test_wall_rollback_does_not_make_elapsed_clock_move_back() -> None:
    clock = FakeClock(5_000, "process-a")
    start = clock.monotonic_mark()
    clock.epoch_nanoseconds = 1_000
    clock.tick_nanoseconds = 20

    assert clock.monotonic_mark().elapsed_nanoseconds(start) == 20


def test_monotonic_mark_cannot_enter_persistence() -> None:
    with pytest.raises(TypeError, match="process-local"):
        pickle.dumps(MonotonicMark("process-a", 1))


def test_dst_fold_and_gap_are_explicit_typed_dispositions() -> None:
    ambiguous = resolve_local_datetime(datetime(2026, 11, 1, 1, 30), "America/New_York")
    nonexistent = resolve_local_datetime(datetime(2026, 3, 8, 2, 30), "America/New_York")

    assert ambiguous.disposition is LocalTimeDisposition.AMBIGUOUS
    assert len(ambiguous.candidates) == 2
    assert nonexistent.disposition is LocalTimeDisposition.NONEXISTENT
    assert nonexistent.candidates == ()


def test_product_selects_a_clock_source_without_starting_resources() -> None:
    source: ClockSource = build_clock_source()

    assert source.now().clock == UNIX_UTC_CLOCK
    assert source.monotonic_mark().process_instance_id


def test_clock_contract_does_not_own_domain_state_machines() -> None:
    source = (ROOT / "contracts/clock.py").read_text(encoding="utf-8")

    for domain_concept in ("CronTask", "WorkflowRun", "LeaseRecord", "RetentionPolicy"):
        assert domain_concept not in source
    assert "TimerManager" not in source

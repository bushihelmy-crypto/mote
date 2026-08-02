"""Canonical time identities for durable and process-local semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ClockIdentity:
    schema_version: Literal[1]
    value: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("clock identity schema is invalid")
        if type(self.value) is not str or not self.value:
            raise ValueError("clock identity is invalid")


UNIX_UTC_CLOCK = ClockIdentity(1, "unix-utc")


@dataclass(frozen=True, slots=True)
class AbsoluteInstant:
    """Timezone-independent durable instant on an explicitly named clock."""

    schema_version: Literal[1]
    clock: ClockIdentity
    epoch_nanoseconds: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported absolute instant schema")
        if not isinstance(self.clock, ClockIdentity):
            raise TypeError("absolute instant requires a ClockIdentity")
        if type(self.epoch_nanoseconds) is not int or self.epoch_nanoseconds < 0:
            raise ValueError("absolute instant epoch must be a non-negative integer")

    @classmethod
    def from_datetime(
        cls,
        value: datetime,
        *,
        clock: ClockIdentity = UNIX_UTC_CLOCK,
    ) -> "AbsoluteInstant":
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("durable instant requires a timezone-aware datetime")
        utc = value.astimezone(timezone.utc)
        seconds = int(utc.timestamp())
        return cls(1, clock, seconds * 1_000_000_000 + utc.microsecond * 1_000)

    def to_datetime(self, *, expected_clock: ClockIdentity) -> datetime:
        self.require_clock(expected_clock)
        seconds, nanoseconds = divmod(self.epoch_nanoseconds, 1_000_000_000)
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanoseconds // 1_000)

    def require_clock(self, expected: ClockIdentity) -> None:
        if self.clock != expected:
            raise ValueError("absolute instant clock identity mismatch")

    def is_at_or_after(self, boundary: "AbsoluteInstant") -> bool:
        self.require_clock(boundary.clock)
        if self.schema_version != boundary.schema_version:
            raise ValueError("absolute instant schema mismatch")
        return self.epoch_nanoseconds >= boundary.epoch_nanoseconds

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "mote.absolute-instant/v1",
            "clock": {
                "schema_version": self.clock.schema_version,
                "value": self.clock.value,
            },
            "epoch_nanoseconds": self.epoch_nanoseconds,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "AbsoluteInstant":
        if type(raw) is not dict or set(raw) != {
            "schema",
            "clock",
            "epoch_nanoseconds",
        }:
            raise ValueError("absolute instant fields are not canonical")
        assert isinstance(raw, dict)
        if raw["schema"] != "mote.absolute-instant/v1":
            raise ValueError("unsupported absolute instant schema")
        clock = raw["clock"]
        if type(clock) is not dict or set(clock) != {"schema_version", "value"}:
            raise ValueError("absolute instant clock fields are not canonical")
        assert isinstance(clock, dict)
        if type(clock["schema_version"]) is not int or clock["schema_version"] != 1:
            raise ValueError("absolute instant clock schema is invalid")
        if type(clock["value"]) is not str:
            raise ValueError("absolute instant clock primitives are invalid")
        if type(raw["epoch_nanoseconds"]) is not int:
            raise ValueError("absolute instant epoch primitive is invalid")
        return cls(
            1,
            ClockIdentity(1, clock["value"]),
            raw["epoch_nanoseconds"],
        )


@dataclass(frozen=True, slots=True)
class MonotonicMark:
    """Process-instance-scoped elapsed-time mark; deliberately not serializable."""

    process_instance_id: str
    tick_nanoseconds: int

    def __post_init__(self) -> None:
        if type(self.process_instance_id) is not str or not self.process_instance_id:
            raise ValueError("monotonic process identity is invalid")
        if type(self.tick_nanoseconds) is not int or self.tick_nanoseconds < 0:
            raise ValueError("monotonic tick is invalid")

    def elapsed_nanoseconds(self, earlier: "MonotonicMark") -> int:
        if self.process_instance_id != earlier.process_instance_id:
            raise ValueError("monotonic marks belong to different process instances")
        elapsed = self.tick_nanoseconds - earlier.tick_nanoseconds
        if elapsed < 0:
            raise ValueError("monotonic source moved backwards")
        return elapsed

    def __reduce__(self):
        raise TypeError("MonotonicMark is process-local and cannot be serialized")


class LocalTimeDisposition(StrEnum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    NONEXISTENT = "nonexistent"


@dataclass(frozen=True, slots=True)
class LocalTimeResolution:
    disposition: LocalTimeDisposition
    candidates: tuple[AbsoluteInstant, ...]


def resolve_local_datetime(
    local: datetime,
    timezone_name: str,
    *,
    clock: ClockIdentity = UNIX_UTC_CLOCK,
) -> LocalTimeResolution:
    """Expose DST ambiguity/nonexistence without choosing domain policy."""
    if local.tzinfo is not None:
        raise ValueError("local datetime must be naive")
    zone = ZoneInfo(timezone_name)
    candidates: list[AbsoluteInstant] = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) != local or round_trip.fold != fold:
            continue
        instant = AbsoluteInstant.from_datetime(aware, clock=clock)
        if instant not in candidates:
            candidates.append(instant)
    if not candidates:
        return LocalTimeResolution(LocalTimeDisposition.NONEXISTENT, ())
    if len(candidates) == 2:
        return LocalTimeResolution(LocalTimeDisposition.AMBIGUOUS, tuple(candidates))
    return LocalTimeResolution(LocalTimeDisposition.EXACT, tuple(candidates))


__all__ = [
    "AbsoluteInstant",
    "ClockIdentity",
    "LocalTimeDisposition",
    "LocalTimeResolution",
    "MonotonicMark",
    "UNIX_UTC_CLOCK",
    "resolve_local_datetime",
]

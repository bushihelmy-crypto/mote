"""Minimal source Port separating durable wall time from local elapsed time."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.clock import AbsoluteInstant, ClockIdentity, MonotonicMark


class ClockSource(Protocol):
    @property
    def durable_clock_identity(self) -> ClockIdentity: ...

    def now(self) -> AbsoluteInstant: ...

    def monotonic_mark(self) -> MonotonicMark: ...


__all__ = ["ClockSource"]

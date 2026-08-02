"""System implementation of the Contracts clock source Port."""

from __future__ import annotations

import time
from uuid import uuid4

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant, ClockIdentity, MonotonicMark


class SystemClock:
    def __init__(self) -> None:
        self._process_instance_id = uuid4().hex

    @property
    def durable_clock_identity(self) -> ClockIdentity:
        return UNIX_UTC_CLOCK

    def now(self) -> AbsoluteInstant:
        return AbsoluteInstant(1, UNIX_UTC_CLOCK, time.time_ns())

    def monotonic_mark(self) -> MonotonicMark:
        return MonotonicMark(self._process_instance_id, time.monotonic_ns())


__all__ = ["SystemClock"]

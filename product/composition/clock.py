"""Product selection of the production clock mechanism."""

from mote.contracts.ports.clock import ClockSource
from mote.runtime.clock import SystemClock


def build_clock_source() -> ClockSource:
    return SystemClock()


__all__ = ["build_clock_source"]

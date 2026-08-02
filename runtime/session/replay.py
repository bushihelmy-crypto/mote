"""Session replay over the canonical deterministic session projection."""

from __future__ import annotations

from dataclasses import dataclass

from mote.runtime.session.log import SessionLog
from mote.runtime.session.projection import SessionProjectionState, reduce_session_envelope
from mote.runtime.telemetry.logging import log_call


@dataclass
class ReplayResult(SessionProjectionState):
    """Projection state rebuilt from a fully verified session journal."""


@log_call(level="DEBUG")
def replay(log: SessionLog) -> ReplayResult:
    """Rebuild one session by applying the same reducer used for live facts."""

    result = ReplayResult()
    for envelope in log.iter_events():
        reduce_session_envelope(result, envelope)
    return result


__all__ = ["ReplayResult", "replay"]

"""Loop-guard subsystem — repeated-failure / no-progress tool-call detection.

The detector is a pure per-Role state machine consumed by ToolResultPolicy.
"""
from __future__ import annotations

from mote.runtime.tools.loop_guard.detector import ThrashDetector, Verdict

__all__ = ["ThrashDetector", "Verdict"]

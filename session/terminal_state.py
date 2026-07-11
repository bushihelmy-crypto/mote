"""Terminal-state recorder — persists the live shell's final env for resume.

The persistent ``Terminal`` tool maintains a live PTY bash subprocess whose cwd,
exported env vars, ``source``d venv, and aliases live *in that process*. That
process is runtime-only (it cannot cross a checkpoint), so a plain resume starts
a clean shell. This recorder captures the shell's final **environment state**
(cwd + env diff relative to the shell's launch baseline) into the same
``rollout.jsonl`` as the session's other events, so resume can re-seed a fresh
shell to that state — *without* re-running any user commands (no replaying
``rm`` / ``install`` / ``push`` side effects).

Mirrors :class:`~mote.session.snapshot.FileSnapshotRecorder`: conforms to
``mote.common.interface.TerminalStateStore``, shares the session's
:class:`SessionLog`, is best-effort (never raises into the tool), and is gated by
``enabled`` (off during resume replay).
"""

from __future__ import annotations

from typing import Dict, List

from mote.common.logs import log_class, logger
from mote.session.events import TerminalStateEvent
from mote.session.log import SessionLog


@log_class(level="DEBUG", exclude={"record"})
class TerminalStateRecorder:
    """Appends the terminal's final cwd + env diff to the session log.

    Conforms to ``mote.common.interface.TerminalStateStore``. Shares the
    session's :class:`SessionLog` so the terminal-state event interleaves with
    the rest of the rollout. ``enabled`` gates recording (off during resume
    replay). Last-write-wins: only the most recent event matters on replay.
    """

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    def record(self, cwd: str, env: Dict[str, str], unset: List[str], *, tool: str = "") -> None:
        """Append a :class:`TerminalStateEvent` (best-effort, never raises)."""
        if not self.enabled:
            return
        try:
            self._log.append(TerminalStateEvent(cwd=cwd, env=dict(env), unset=list(unset), tool=tool))
        except Exception as exc:  # noqa: BLE001 — recording must not break the tool
            logger.warning(f"TerminalStateRecorder: failed to record terminal state: {exc}")


__all__ = ["TerminalStateRecorder"]

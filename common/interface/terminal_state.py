"""TerminalStateStore protocol — the persistent-terminal state capture slice.

The narrow face the ``Terminal`` tool uses to record the *final environment
state* of its live PTY shell (cwd + the env diff relative to the shell's launch
baseline) just before yielding, without importing the concrete ``session``
implementation.

Why a Protocol here (not in ``session``): the ``executor`` layer must never
import the ``roles`` layer (the strict downward-only layering rule). The concrete
``TerminalStateRecorder`` lives in ``session`` and is *injected* into the tool as
a Role capability (``record_terminal_state``); the tool only depends on this
structural face, so no upward import is introduced.

Mirrors :class:`~mote.common.interface.FileSnapshotStore`: a leaf module that
only needs ``typing``, importable from anywhere without risking a cycle.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class TerminalStateStore(Protocol):
    """Records the final environment state of a persistent terminal shell.

    Implemented by ``session.TerminalStateRecorder`` (production) and any test
    double. Called by the ``Terminal`` tool after a call returns to a prompt
    (the shell is idle), with the captured cwd + env diff. The store appends a
    terminal-state event (last-write-wins) to the rollout. It owns its own
    enable/disable and persistence, and must be cheap and non-throwing from the
    tool's point of view.
    """

    def record(self, cwd: str, env: Dict[str, str], unset: List[str], *, tool: str = "") -> None:
        """Record the terminal's final cwd + env diff.

        Args:
            cwd: The shell's current working directory.
            env: The added/changed env vars relative to the launch baseline.
            unset: The env var names present at launch but unset since.
            tool: Name of the tool performing the capture (for the record).
        """
        ...

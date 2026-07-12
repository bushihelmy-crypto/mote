"""Kernel-state recorder — persists the live kernel's final env for resume.

The persistent ``Python`` tool maintains a live Jupyter (ipykernel) process whose
cwd and ``os.environ`` mutations live *in that process*. That process is
runtime-only (it cannot cross a checkpoint), so a plain resume starts a clean
kernel. This recorder captures the kernel's final **environment state** (cwd +
env diff relative to the kernel's launch baseline) into the same ``rollout.jsonl``
as the session's other events, so resume can re-seed a fresh kernel to that
state — *without* re-running any user code (no replaying side effects).

Note: only cwd + env vars are captured. The kernel's Python namespace
(variables/imports/functions) is NOT preserved; the model re-sees its prior code
in the replayed message history and re-establishes any state it needs.

Mirrors :class:`~metagpt.session.terminal_state.TerminalStateRecorder`: conforms
to ``metagpt.common.interface.KernelStateStore``, shares the session's
:class:`SessionLog`, is best-effort (never raises into the tool), and is gated by
``enabled`` (off during resume replay).
"""

from __future__ import annotations

from typing import Dict, List

from metagpt.common.logs import log_class, logger
from metagpt.session.events import KernelStateEvent
from metagpt.session.log import SessionLog


@log_class(level="DEBUG", exclude={"record"})
class KernelStateRecorder:
    """Appends the kernel's final cwd + env diff to the session log.

    Conforms to ``metagpt.common.interface.KernelStateStore``. Shares the
    session's :class:`SessionLog` so the kernel-state event interleaves with the
    rest of the rollout. ``enabled`` gates recording (off during resume replay).
    Last-write-wins: only the most recent event matters on replay. Independent of
    the terminal-state event so a session's shell and kernel restore separately.
    """

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    def record(self, cwd: str, env: Dict[str, str], unset: List[str], *, tool: str = "") -> None:
        """Append a :class:`KernelStateEvent` (best-effort, never raises)."""
        if not self.enabled:
            return
        try:
            self._log.append(
                KernelStateEvent(cwd=cwd, env=dict(env), unset=list(unset), tool=tool)
            )
        except Exception as exc:  # noqa: BLE001 — recording must not break the tool
            logger.warning(f"KernelStateRecorder: failed to record kernel state: {exc}")


__all__ = ["KernelStateRecorder"]

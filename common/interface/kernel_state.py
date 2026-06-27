"""KernelStateStore protocol — the persistent-kernel state capture slice.

The narrow face the ``Python`` tool uses to record the *final environment state*
of its live Jupyter kernel (cwd + the env diff relative to the kernel's launch
baseline) just after a cell settles, without importing the concrete ``session``
implementation.

Why a Protocol here (not in ``session``): the ``executor`` layer must never
import the ``roles`` layer (the strict downward-only layering rule). The concrete
``KernelStateRecorder`` lives in ``session`` and is *injected* into the tool as a
Role capability (``record_kernel_state``); the tool only depends on this
structural face, so no upward import is introduced.

Mirrors :class:`~metagpt.common.interface.TerminalStateStore`: a leaf module that
only needs ``typing``, importable from anywhere without risking a cycle. Kept a
separate type (rather than reusing ``TerminalStateStore``) because the kernel and
terminal restores are independent — they re-seed different processes via
different mechanisms and must not clobber each other on replay.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class KernelStateStore(Protocol):
    """Records the final environment state of a persistent Python kernel.

    Implemented by ``session.KernelStateRecorder`` (production) and any test
    double. Called by the ``Python`` tool after a cell returns to idle, with the
    captured cwd + env diff. The store appends a kernel-state event
    (last-write-wins) to the rollout. It owns its own enable/disable and
    persistence, and must be cheap and non-throwing from the tool's point of
    view.

    Note: only cwd + environment variables are captured — NOT the kernel's
    Python namespace (variables/imports/functions). Those are left for the model
    to re-establish from the replayed message history; no code is auto-rerun.
    """

    def record(self, cwd: str, env: Dict[str, str], unset: List[str], *, tool: str = "") -> None:
        """Record the kernel's final cwd + env diff.

        Args:
            cwd: The kernel process's current working directory.
            env: The added/changed env vars relative to the launch baseline.
            unset: The env var names present at launch but unset since.
            tool: Name of the tool performing the capture (for the record).
        """
        ...

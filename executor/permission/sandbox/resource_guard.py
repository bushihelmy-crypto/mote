"""ResourceGuard — live, mutable resource-limit state for one session.

The cgroup analogue of :class:`SandboxGuard`. Where ``SandboxGuard`` holds the
mutable *filesystem* boundary (writable roots, session grants) the runtime reads
fresh per command, ``ResourceGuard`` holds the mutable *resource* caps (memory /
pids / cpu) the runtime reads fresh per command via its ``limits_provider``.

Seeded from a :class:`SandboxRuntimeConfig`'s static caps, then adjustable at
session-time (e.g. an interactive "raise memory to 8G"): the next wrapped command
picks the new cap up without rebuilding the runtime — mirroring how a
``SandboxGuard.add_session_root`` grant takes effect on the next command.
"""
from __future__ import annotations

from typing import Optional

from mote.common.schema import SandboxRuntimeConfig
from mote.sandbox.resources import ResourceLimits


class ResourceGuard:
    """Hold the live :class:`ResourceLimits` for one Role session."""

    def __init__(self, config: SandboxRuntimeConfig) -> None:
        # Seed from the declarative config's static caps. ``memory_swap_max``
        # keeps the ResourceLimits default (swap disabled when memory is capped).
        self._limits = ResourceLimits(
            memory_max=config.memory_max,
            pids_max=config.pids_max,
            cpu_quota=config.cpu_quota,
        )

    def limits(self) -> ResourceLimits:
        """The live resource caps (read fresh by the runtime each command)."""
        return self._limits

    def set_memory_max(self, value: Optional[str]) -> None:
        """Adjust the memory cap for the rest of the session (``None`` = uncap)."""
        self._limits.memory_max = value

    def set_pids_max(self, value: Optional[int]) -> None:
        """Adjust the PID/task cap for the rest of the session (``None`` = uncap)."""
        self._limits.pids_max = value

    def set_cpu_quota(self, value: Optional[str]) -> None:
        """Adjust the CPU quota for the rest of the session (``None`` = uncap)."""
        self._limits.cpu_quota = value


__all__ = ["ResourceGuard"]

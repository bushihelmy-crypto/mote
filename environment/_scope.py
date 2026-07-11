#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ScopedExitMixin — shared sync+async context-manager protocol for RAII guards.

The control plane has three RAII-style resources that all need to release
themselves when a ``with`` / ``async with`` block exits without an explicit
commit: :class:`~mote.environment.registry.SpawnReservation`,
:class:`~mote.environment.residency.ResidencySlot`, and
:class:`~mote.environment.limiter.AgentExecutionGuard`. They differ only in
*what* they release, so the four context-manager dunders are factored here and
each subclass implements a single :meth:`_scope_exit` hook.
"""

from __future__ import annotations


class ScopedExitMixin:
    """Mixin providing sync + async context-manager protocol.

    On leaving the block (either flavor), :meth:`_scope_exit` runs. Subclasses
    override it to release/roll back whatever they hold; it must be idempotent
    (an explicit ``commit``/``release`` typically makes it a no-op).
    """

    def _scope_exit(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self._scope_exit()
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        self._scope_exit()
        return False


__all__ = ["ScopedExitMixin"]

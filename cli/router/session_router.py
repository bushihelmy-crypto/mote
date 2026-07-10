#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SessionRouter`` — inbound → session routing for multi-tenant gateways (§7.2).

A documented phase-② stub. The single-session spine (§1–§6) assumes one
:class:`~metagpt.cli.driver.SessionDriver`; a public-platform gateway fans a flood
of inbound messages out to *per-user* sessions. ``SessionRouter`` sits upstream of
the driver: a pluggable ``key_fn`` maps each inbound message to a routing key
(openid / handle / email-thread id), the first message from a new key lazily
spawns a driver, and idle sessions are reclaimed by the ``environment`` layer's
existing LRU residency (reuse, never rebuild).

The method bodies below raise :class:`NotImplementedError`: the *shape* is fixed
by §7.2, the wiring (``BroadcastPort`` fan-in, durable ``user_id → session_id``
mapping, concurrency isolation) lands in a later phase. Importing this module is
side-effect-free so the package stays loadable in phase ①.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


def _default_key(msg: Any) -> str:
    """Default routing key: the inbound message's ``user_id`` (§7.2)."""
    return getattr(msg, "user_id", "")


class SessionRouter:
    """Route inbound platform messages to per-user sessions (lazy + resident).

    ``driver_factory(key, msg) -> SessionDriver`` builds (or resumes) the session
    for a freshly seen routing key; ``key_fn(msg) -> str`` is the pluggable
    user→key map. Both are injected so the router stays platform-agnostic.
    """

    def __init__(
        self,
        control: Any,
        driver_factory: Callable[[str, Any], Any],
        *,
        key_fn: Callable[[Any], str] = _default_key,
    ) -> None:
        self._control = control
        self._driver_factory = driver_factory
        self._key_fn = key_fn
        self._drivers: Dict[str, Any] = {}

    async def on_message(self, msg: Any) -> None:
        """Fan one inbound message to its session driver (lazy-create on first sight)."""
        raise NotImplementedError("SessionRouter.on_message is a phase-② stub (§7.2)")

    def _spawn(self, key: str, msg: Any) -> Any:
        """Lazily build + register the driver for a new routing key."""
        raise NotImplementedError("SessionRouter._spawn is a phase-② stub (§7.2)")

    def get_driver(self, key: str) -> Optional[Any]:
        """Return the live driver for *key*, or ``None`` if not yet spawned."""
        return self._drivers.get(key)


__all__ = ["SessionRouter"]

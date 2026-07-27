#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SessionRegistry`` — resident ``session_id → {control, role}`` for N sessions.

The multi-session host's equivalent of ``build_app``'s single resident role: a
network process serves many threads/connections, each addressed by its
``session_id`` (AG-UI ``threadId`` / ACP session id). This registry keeps each
one's ``{control, role}`` alive across turns (an AG-UI ``POST /run`` is one
turn against a *persistent* server-side thread), minting new ones on demand from
the shared :class:`~mote.product.cli.app.EngineBuild` so the construction path is
byte-identical to the single-session host (§4 template — no parallel bootstrap).

``get_or_create`` mints-or-returns; ``evict`` tears one down through the shared
Runtime lifecycle protocol (``control.stop`` + ``Engine.release``/Role cleanup).
Concurrency-safe under asyncio: creation
is guarded by an :class:`asyncio.Lock` so two concurrent first-touches of the
same id share one session (never two controls for one thread).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from mote.product.cli import backend
from mote.runtime.lifecycle import LifecyclePhase, LifecycleStack
from mote.runtime.logging import logger


@dataclass
class ResidentSession:
    """One live session: its control plane, its root role, and that role's id.

    The unit :class:`SessionRegistry` keeps resident across turns. ``agent_id``
    is the root runtime's id (== ``role.session_id``) — the address a
    :class:`~mote.product.cli.serving.connection_scope.ConnectionScope` drives a turn
    against via ``control.send_input(agent_id, msg)``.
    """

    session_id: str
    control: Any
    role: Any
    agent_id: str
    lifecycle: LifecycleStack = field(default_factory=LifecycleStack, repr=False)
    lifecycle_prepared: bool = field(default=False, repr=False)


class SessionRegistry:
    """Mint + hold resident sessions by ``session_id``, sharing one engine.

    Built from a ``role_factory`` (the :class:`~mote.product.cli.app.EngineBuild`
    closure) so every session shares the one loaded ``config`` + engine
    ``context``; each ``get_or_create`` builds a role (resuming its rollout when
    the id names a persisted session), wires a control plane via
    ``backend.build_control``, starts it, and caches it. Idempotent per id.
    """

    def __init__(
        self,
        role_factory: Callable[..., Any],
        *,
        name: str = "Assistant",
        engine: Any = None,
    ) -> None:
        self._role_factory = role_factory
        self._engine = engine
        self._name = name
        self._sessions: Dict[str, ResidentSession] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    def get(self, session_id: str) -> Optional[ResidentSession]:
        """Return the resident session for *session_id*, or ``None`` if absent."""
        return self._sessions.get(session_id)

    @property
    def session_ids(self) -> list:
        return list(self._sessions.keys())

    async def get_or_create(self, session_id: Optional[str] = None) -> ResidentSession:
        """Return the resident session for *session_id*, minting it if absent.

        A known id returns the live session (resident across turns). An unknown
        id — or ``None`` (a brand-new thread) — builds a fresh role sharing the
        engine, wires + starts its control plane, and caches it. When
        *session_id* names a persisted rollout, the built role resumes it so the
        thread continues where it left off. Concurrency-safe: two racing
        first-touches of one id share the single session the lock lets through.
        """
        if self._closing:
            raise RuntimeError("SessionRegistry is closing and cannot create sessions")
        if session_id is not None:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
        async with self._lock:
            if self._closing:
                raise RuntimeError("SessionRegistry is closing and cannot create sessions")
            # Re-check under the lock: a concurrent caller may have created it
            # while we awaited the lock (double-checked locking).
            if session_id is not None:
                existing = self._sessions.get(session_id)
                if existing is not None:
                    return existing
            session = self._build(session_id)
            self._sessions[session.session_id] = session
            return session

    def _build(self, session_id: Optional[str]) -> ResidentSession:
        """Construct + start one resident session (role → control → start)."""
        role = self._role_factory(name=self._name, session_id=session_id)
        if role is None:  # pragma: no cover — factory only returns None on typed-agent miss
            raise ValueError(f"role_factory returned None for session_id={session_id!r}")
        # Resume a persisted rollout when the id names one, so a returning thread
        # continues its history rather than starting blank. Best-effort: a fresh
        # id simply has no rollout (resume returns False) and starts clean.
        if session_id is not None:
            try:
                backend.resume_role(role)
            except Exception as exc:  # noqa: BLE001 — resume is best-effort
                logger.warning(f"SessionRegistry: resume failed for {session_id[:8]}: {exc}")
        return self._start(role)

    def adopt(self, role: Any) -> ResidentSession:
        """Register an ALREADY-built role as a resident session (the fork path).

        Where ``get_or_create`` mints a role from the factory, ``adopt`` takes a
        role produced elsewhere — e.g. ``backend.fork_role`` branching a sibling
        off a live session — wires + starts its control plane, and caches it
        under its own session id. Idempotent per id (a re-adopt of a resident
        role returns the existing session rather than double-starting it).
        """
        if self._closing:
            raise RuntimeError("SessionRegistry is closing and cannot adopt sessions")
        agent_id = backend.role_session_id(role)
        existing = self._sessions.get(agent_id)
        if existing is not None:
            return existing
        session = self._start(role)
        self._sessions[session.session_id] = session
        return session

    @staticmethod
    def _start(role: Any) -> ResidentSession:
        """Wire + start a control plane over *role*, returning its resident session."""
        control, _ = backend.build_control(role)
        control.start()
        agent_id = backend.role_session_id(role)
        return ResidentSession(session_id=agent_id, control=control, role=role, agent_id=agent_id)

    async def evict(self, session_id: str) -> bool:
        """Tear down + drop one resident session. Returns whether it existed.

        Stops the control plane and runs the role's cleanup. A failed resource
        keeps the session resident so a later eviction retries only that
        resource. A no-op for an unknown id.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        await self._teardown(session)
        async with self._lock:
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id)
        return True

    async def aclose(self) -> None:
        """Evict every resident session (host shutdown)."""
        async with self._lock:
            self._closing = True
            sessions = list(self._sessions.values())
        failures: list[tuple[str, BaseException]] = []
        for session in sessions:
            try:
                await self._teardown(session)
            except Exception as exc:
                failures.append((session.session_id, exc))
            else:
                async with self._lock:
                    if self._sessions.get(session.session_id) is session:
                        self._sessions.pop(session.session_id)
        if failures:
            details = "; ".join(f"{session_id}: {type(exc).__name__}: {exc}" for session_id, exc in failures)
            raise RuntimeError(f"SessionRegistry shutdown failed: {details}")
        if self._engine is not None:
            await self._engine.aclose()

    async def _teardown(self, session: ResidentSession) -> None:
        if not session.lifecycle_prepared:
            session.lifecycle_prepared = True
            if self._engine is not None:
                session.lifecycle.register_close(
                    "engine-agent",
                    lambda: self._engine.release(session.role),
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
            else:
                cleanup = backend.role_cleanup(session.role)
                if cleanup is not None:
                    session.lifecycle.register_close(
                        "role",
                        cleanup,
                        phase=LifecyclePhase.CLOSE_RESOURCES,
                    )
            session.lifecycle.register_close(
                "control-plane",
                session.control.stop,
                phase=LifecyclePhase.STOP_PRODUCERS,
            )
        await session.lifecycle.aclose()


__all__ = ["SessionRegistry", "ResidentSession"]

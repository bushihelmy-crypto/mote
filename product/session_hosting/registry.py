#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SessionRegistry`` — resident ``session_id → {control, role}`` for N sessions.

The multi-session host's equivalent of ``build_app``'s single resident role: a
network process serves many threads/connections, each addressed by its
``session_id`` (AG-UI ``threadId`` / ACP session id). This registry keeps each
one's ``{control, role}`` alive across turns (an AG-UI ``POST /run`` is one
turn against a *persistent* server-side thread), minting new ones on demand from
the shared :func:`~mote.product.composition.bootstrap.activate_application` result so the construction path is
byte-identical to the single-session host (§4 template — no parallel bootstrap).

``create`` mints a hosted session; ``evict`` tears one down through the shared
Runtime lifecycle protocol (``control.stop`` + ``Engine.release``/Role cleanup).
Concurrency-safe under asyncio: creation
is guarded by an :class:`asyncio.Lock` so two concurrent first-touches of the
same id share one session (never two controls for one thread).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import Token
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

from mote.contracts.agent import RunnableAgent
from mote.contracts.ports.agent.hosting import ResidentAgentHostingSnapshot
from mote.contracts.ports.interaction.role import RoleHumanInteractionPort
from mote.contracts.session import SessionHostingError, SessionHostingErrorKind, SessionLifecycleState
from mote.contracts.session.identity import SessionId
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.product.config.schema import Config
from mote.product.session_hosting.composition import compose_resident_agent
from mote.runtime.agent.role import Role
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleStack
from mote.runtime.engine import EngineAgentRequest
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.models.clients.context import Context
from mote.runtime.session.lifecycle import SessionLifecycleStore
from mote.runtime.telemetry.logging import logger

OutputT = TypeVar("OutputT")


class HostedAgent(RunnableAgent[OutputT], Protocol[OutputT]):
    """Product-owned capabilities required to host one resident Agent."""

    config: Config

    def resident_hosting_snapshot(self) -> ResidentAgentHostingSnapshot: ...

    @property
    def state(self) -> RoleState: ...

    @property
    def telemetry(self) -> TelemetryRuntime: ...

    def resume_session(self) -> bool: ...

    async def fork_session(self) -> "HostedAgent[OutputT]": ...

    def bind_human_interaction(
        self, interaction: RoleHumanInteractionPort
    ) -> Token[RoleHumanInteractionPort | None]: ...

    def reset_human_interaction(self, token: Token[RoleHumanInteractionPort | None]) -> None: ...


class HostedAgentOwner(Protocol[OutputT]):
    """Minimal Application lifecycle used by the session host."""

    async def release(self, agent: HostedAgent[OutputT]) -> None: ...

    async def aclose(self) -> None: ...


def _build_control(role: HostedAgent[OutputT]) -> tuple[AgentControl, AgentRuntime[OutputT]]:
    hosting = role.resident_hosting_snapshot()
    workspace_root = hosting.workspace_root
    return compose_resident_agent(
        role,
        residency_dir=workspace_root / ".agent_residency",
        sessions_dir=workspace_root / ".agent_sessions",
        writer=hosting.writer,
        governance=role.config.agents,
        budget=hosting.budget,
        workflow_governance=hosting.workflow_governance,
        workflow_delivery=hosting.workflow_delivery,
    )


def _resume_role(role: HostedAgent[OutputT]) -> bool:
    return role.resume_session()


def _role_cleanup(role: HostedAgent[OutputT]) -> Callable[[], Awaitable[None]]:
    """Typed lifecycle seam retained for deterministic fault injection."""

    return role.cleanup


@dataclass
class ResidentSession(Generic[OutputT]):
    """One live session: its control plane, its root role, and that role's id.

    The unit :class:`SessionRegistry` keeps resident across turns. ``agent_id``
    is the root runtime's id (== ``role.session_id``) — the address a
    :class:`~mote.product.session_hosting.connection.ConnectionScope` drives a turn
    against via ``control.send_input(agent_id, msg)``.
    """

    session_id: str
    control: AgentControl
    role: HostedAgent[OutputT]
    runtime: AgentRuntime[OutputT]
    agent_id: str
    lifecycle: LifecycleStack = field(default_factory=LifecycleStack, repr=False)
    lifecycle_prepared: bool = field(default=False, repr=False)


class SessionRegistry(Generic[OutputT]):
    """Mint + hold resident sessions by ``session_id``, sharing one engine.

    Built from the canonical Product Application ``role_factory``
    closure, so every session shares the one loaded ``config`` + engine
    ``context``; each ``create`` builds a role (resuming its rollout when
    the id names a persisted session), wires a control plane via
    ``backend.build_control``, starts it, and caches it. Idempotent per id.
    """

    def __init__(
        self,
        role_factory: Callable[[EngineAgentRequest], HostedAgent[OutputT]],
        *,
        name: str = "Assistant",
        engine: HostedAgentOwner[OutputT] | None = None,
    ) -> None:
        self._role_factory = role_factory
        self._engine = engine
        self._name = name
        self._sessions: dict[str, ResidentSession[OutputT]] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    def get(self, session_id: str) -> ResidentSession[OutputT] | None:
        """Return the resident session for *session_id*, or ``None`` if absent."""
        return self._sessions.get(session_id)

    @property
    def session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def create_new(self) -> ResidentSession[OutputT]:
        """Create a new empty Session; this is the only minting operation."""
        if self._closing:
            raise RuntimeError("SessionRegistry is closing and cannot create sessions")
        async with self._lock:
            if self._closing:
                raise RuntimeError("SessionRegistry is closing and cannot create sessions")
            session = self._build(None)
            self._sessions[session.session_id] = session
            return session

    def get_resident(self, session_id: str) -> ResidentSession[OutputT] | None:
        return self._sessions.get(session_id)

    async def load_existing(self, session_id: str) -> ResidentSession[OutputT]:
        """Load a verified durable Session; never creates an empty substitute."""
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            role = self._role_factory(EngineAgentRequest(name=self._name, session_id=session_id))
            try:
                resumed = _resume_role(role)
            except Exception as exc:
                raise SessionHostingError(SessionHostingErrorKind.LOAD_FAILED, session_id, str(exc)) from exc
            if not resumed:
                raise SessionHostingError(
                    SessionHostingErrorKind.NOT_FOUND, session_id, "durable session does not exist"
                )
            session = self._start(role)
            self._sessions[session_id] = session
            return session

    async def get_resident_or_load(self, session_id: str) -> ResidentSession[OutputT]:
        resident = self.get_resident(session_id)
        return resident if resident is not None else await self.load_existing(session_id)

    async def fork_existing(self, session_id: str) -> ResidentSession[OutputT]:
        source = await self.get_resident_or_load(session_id)
        try:
            forked = await source.role.fork_session()
        except NotImplementedError as exc:
            raise SessionHostingError(SessionHostingErrorKind.FORK_UNSUPPORTED, session_id, str(exc)) from exc
        except Exception as exc:
            raise SessionHostingError(SessionHostingErrorKind.FORK_FAILED, session_id, str(exc)) from exc
        if forked is None:
            raise SessionHostingError(
                SessionHostingErrorKind.FORK_UNSUPPORTED, session_id, "Agent does not support fork"
            )
        return self.adopt(forked)

    def _build(self, session_id: str | None) -> ResidentSession[OutputT]:
        """Construct + start one resident session (role → control → start)."""
        role = self._role_factory(EngineAgentRequest(name=self._name, session_id=session_id))
        if role is None:  # pragma: no cover — factory only returns None on typed-agent miss
            raise ValueError(f"role_factory returned None for session_id={session_id!r}")
        session = self._start(role)
        self._activate_lifecycle(role)
        return session

    def adopt(self, role: HostedAgent[OutputT]) -> ResidentSession[OutputT]:
        """Register an ALREADY-built role as a resident session (the fork path).

        Where ``create`` mints a role from the factory, ``adopt`` takes a
        role produced elsewhere — e.g. ``backend.fork_role`` branching a sibling
        off a live session — wires + starts its control plane, and caches it
        under its own session id. Idempotent per id (a re-adopt of a resident
        role returns the existing session rather than double-starting it).
        """
        if self._closing:
            raise RuntimeError("SessionRegistry is closing and cannot adopt sessions")
        agent_id = role.session_id
        existing = self._sessions.get(agent_id)
        if existing is not None:
            return existing
        session = self._start(role)
        self._activate_lifecycle(role)
        self._sessions[session.session_id] = session
        return session

    @staticmethod
    def _activate_lifecycle(role: HostedAgent[OutputT]) -> None:
        """Activate the canonical durable lifecycle before exposing residency."""
        if not isinstance(role, Role):
            return
        root = role.resident_hosting_snapshot().workspace_root
        SessionLifecycleStore(root / "session-lifecycle.sqlite3").activate(SessionId(role.session_id))

    @staticmethod
    def _start(role: HostedAgent[OutputT]) -> ResidentSession[OutputT]:
        """Wire + start a control plane over *role*, returning its resident session."""
        control, runtime = _build_control(role)
        control.start()
        agent_id = role.session_id
        return ResidentSession(
            session_id=agent_id,
            control=control,
            role=role,
            runtime=runtime,
            agent_id=agent_id,
        )

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
        self._begin_draining(session)
        await self._teardown(session)
        async with self._lock:
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id)
        return True

    @staticmethod
    def _begin_draining(session: ResidentSession[OutputT]) -> None:
        if not isinstance(session.role, Role):
            return
        root = session.role.resident_hosting_snapshot().workspace_root
        store = SessionLifecycleStore(root / "session-lifecycle.sqlite3")
        snapshot = store.get(SessionId(session.session_id))
        if snapshot.state is SessionLifecycleState.ACTIVE:
            store.set_state(
                SessionId(session.session_id),
                SessionLifecycleState.DRAINING,
                expected_generation=snapshot.lifecycle_generation,
                expected_revision=snapshot.revision,
            )

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

    async def _teardown(self, session: ResidentSession[OutputT]) -> None:
        if not session.lifecycle_prepared:
            session.lifecycle_prepared = True
            if self._engine is not None:
                engine = self._engine
                session.lifecycle.register_close(
                    "engine-agent",
                    lambda: engine.release(session.role),
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
            else:
                session.lifecycle.register_close(
                    "role",
                    _role_cleanup(session.role),
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
            session.lifecycle.register_close(
                "control-plane",
                session.control.stop,
                phase=LifecyclePhase.STOP_PRODUCERS,
            )
        await session.lifecycle.aclose()


__all__ = ["SessionRegistry", "ResidentSession"]

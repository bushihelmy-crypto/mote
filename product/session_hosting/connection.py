#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ConnectionScope`` — per-connection / per-turn presentation bundle (§Phase 0).

The multi-session dual of ``SessionDriver.run()``'s single-session loop. Where
the terminal driver owns ONE resident ``{port, projector, consumers}`` for the
process lifetime, a network host mints a fresh scope per connection (or per
turn) bound to a :class:`~mote.product.session_hosting.registry.ResidentSession`
pulled from the shared registry:

    scope = ConnectionScope(session, consumers=[agui_consumer], port=agui_port)
    async with scope:                       # subscribe projector to role telemetry
        await scope.run_turn(user_message)  # drive one turn → events fan out
                                            # → aclose consumers on exit

Each scope has its OWN :class:`BaseProjector` (with its own consumers +
capability adapters) subscribed to the session's Role Telemetry, so two concurrent
connections' event streams never interleave — a turn driven here fans out only
to *this* scope's consumers. The engine (``control`` / ``role``) is shared and
untouched; only the presentation edge is per-connection (§4 template).

This is transport-free: it takes already-built consumers + an optional port and
drives turns. The AG-UI / ACP transports (``consumers/agui/`` etc.) construct
the consumer + port and own the socket; they lean on this to run turns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextvars import Token
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Optional
from uuid import uuid4

from mote.contracts.conversation import Message
from mote.contracts.ports.events.telemetry import (
    TelemetryIdentity,
    TelemetryOverflow,
    TelemetrySubscription,
    TelemetrySubscriptionSpec,
)
from mote.contracts.ports.interaction.role import RoleHumanInteractionPort
from mote.product.interaction.human_channel import PortHumanChannel
from mote.product.interaction.ports import InputPort
from mote.product.interaction.turn import TurnRunner, format_turn_error
from mote.product.presentation.input_events import PRESENTATION_INPUT_TYPES
from mote.product.presentation.projection.base import BaseProjector, PresentationConsumer
from mote.product.presentation.projection.projector import ViewProjector
from mote.product.session_hosting.registry import ResidentSession
from mote.runtime.telemetry.logging import logger

_format_turn_error = format_turn_error


class ConnectionLifecycleState(StrEnum):
    NEW = "new"
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"


class ConnectionCleanupPhase(StrEnum):
    TELEMETRY = "telemetry"
    HUMAN_BINDING = "human_binding"
    PROJECTOR = "projector"
    PORT = "port"


class ConnectionCloseDisposition(StrEnum):
    SETTLED = "settled"
    TIMED_OUT = "timed_out"
    CLEANUP_FAILED = "cleanup_failed"


class ConnectionTimeoutPolicy(StrEnum):
    RETAIN_DRAINING = "retain_draining"


@dataclass(frozen=True, slots=True)
class ConnectionCleanupReceipt:
    generation: str
    state: ConnectionLifecycleState
    settled: tuple[ConnectionCleanupPhase, ...]
    failed: tuple[ConnectionCleanupPhase, ...]
    disposition: ConnectionCloseDisposition = ConnectionCloseDisposition.SETTLED


class ConnectionScope:
    """One connection's presentation edge over a shared resident session.

    Holds a per-connection :class:`BaseProjector` (own consumers + adapters)
    subscribed to the session's Role Telemetry, plus an optional input port bound as
    the role's human channel for the scope's lifetime. ``run_turn`` drives one
    turn against the resident control plane and lets output flow out through the
    projector — never by reading ``context.messages`` (symmetric with the
    terminal driver's §0.2 invariant).

    Use as an async context manager: ``__aenter__`` subscribes + binds,
    ``__aexit__`` unsubscribes + ``aclose``\\es the consumers/port. The shared
    engine is left running (the registry owns its lifecycle).
    """

    def __init__(
        self,
        session: ResidentSession[str],
        *,
        consumers: Iterable[PresentationConsumer] | None = None,
        port: InputPort | None = None,
        quiescent_poll_interval: float = 0.05,
        cleanup_timeout: float = 5.0,
    ) -> None:
        self._session = session
        self._control = session.control
        self._agent_id = session.agent_id
        self._role = session.role
        self._port = port
        self._projector = BaseProjector(consumers or [], projector=ViewProjector())
        self._turn_runner = TurnRunner(
            self._control,
            self._agent_id,
            self._projector,
            quiescent_poll_interval=quiescent_poll_interval,
        )
        self._telemetry_handles: list[TelemetrySubscription] = []
        self._telemetry_identity = TelemetryIdentity(f"mote.product.cli.connection_scope.{uuid4().hex}")
        self._human_token: Token[RoleHumanInteractionPort | None] | None = None
        self._env_bound = False
        self._generation = uuid4().hex
        self._state = ConnectionLifecycleState.NEW
        self._pending_cleanup: set[ConnectionCleanupPhase] = set()
        if cleanup_timeout <= 0:
            raise ValueError("connection cleanup timeout must be positive")
        self._cleanup_timeout = cleanup_timeout
        self._timeout_policy = ConnectionTimeoutPolicy.RETAIN_DRAINING

    @property
    def projector(self) -> BaseProjector:
        return self._projector

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ------------------------------------------------------------------
    # Lifecycle (async context manager)
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "ConnectionScope":
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        receipt = await self.close_with_timeout()
        if receipt.state is not ConnectionLifecycleState.CLOSED:
            phases = ",".join(phase.value for phase in receipt.failed)
            raise RuntimeError(f"connection cleanup incomplete for {receipt.generation}: {phases}")

    async def close_with_timeout(self) -> ConnectionCleanupReceipt:
        """Apply the Product surface's bounded retain-DRAINING close policy."""
        try:
            async with asyncio.timeout(self._cleanup_timeout):
                return await self.aclose()
        except asyncio.TimeoutError:
            self._state = ConnectionLifecycleState.DRAINING
            return ConnectionCleanupReceipt(
                self._generation,
                self._state,
                (),
                tuple(sorted(self._pending_cleanup, key=lambda phase: phase.value)),
                ConnectionCloseDisposition.TIMED_OUT,
            )

    async def open(self) -> None:
        """Subscribe this scope's projector to telemetry and bind the port.

        Binding the human channel is scoped: the previous ``env`` is stashed and
        restored on :meth:`aclose`, so overlapping scopes on the same role (rare,
        but possible for a resumed thread) don't clobber each other's channel.
        """
        if self._state is not ConnectionLifecycleState.NEW:
            raise RuntimeError(f"connection generation {self._generation} cannot activate from {self._state.value}")
        if not self._telemetry_handles:
            try:
                for ordinal, event_type in enumerate(PRESENTATION_INPUT_TYPES):
                    handle = await self._role.telemetry.subscribe_typed(
                        TelemetrySubscriptionSpec(
                            identity=TelemetryIdentity(f"{self._telemetry_identity}.{ordinal}"),
                            capacity=4096,
                            overflow=TelemetryOverflow.DROP_OLDEST,
                        ),
                        event_type,
                        self._projector,
                        self._projector,
                    )
                    self._telemetry_handles.append(handle)
                self._pending_cleanup.add(ConnectionCleanupPhase.TELEMETRY)
            except Exception as exc:  # noqa: BLE001 — connection activation is fail-closed
                if self._telemetry_handles:
                    self._pending_cleanup.add(ConnectionCleanupPhase.TELEMETRY)
                    self._state = ConnectionLifecycleState.DRAINING
                raise RuntimeError("connection presentation activation failed") from exc
        self._pending_cleanup.add(ConnectionCleanupPhase.PROJECTOR)
        if self._port is not None:
            try:
                self._human_token = self._role.bind_human_interaction(PortHumanChannel(self._port))
            except Exception as exc:
                self._pending_cleanup.add(ConnectionCleanupPhase.PORT)
                self._state = ConnectionLifecycleState.DRAINING
                raise RuntimeError("connection human binding activation failed") from exc
            else:
                self._env_bound = True
                self._pending_cleanup.add(ConnectionCleanupPhase.HUMAN_BINDING)
                self._pending_cleanup.add(ConnectionCleanupPhase.PORT)
        self._state = ConnectionLifecycleState.ACTIVE

    async def run_turn(self, message: Message) -> None:
        """Drive one turn: inject *message*, await quiescence, fan output out.

        *message* is a backend ``UserMessage`` (built by the transport via
        ``backend.turn_message``). Surfaces the user's own turn as a
        ``MessageBlockCompleted`` ViewEvent first (symmetric with the terminal
        driver, so a network transcript shows the prompt), then delivers it to
        the control plane and polls to quiescence. A turn that ends ERRORED
        surfaces an ``ErrorRaised`` (never by reading history).
        """
        if self._state is not ConnectionLifecycleState.ACTIVE:
            raise RuntimeError(f"connection generation {self._generation} does not accept turns")
        await self._turn_runner.run(message)

    async def aclose(self) -> ConnectionCleanupReceipt:
        """Unsubscribe the projector, restore the role env, close consumers/port.

        The shared engine (``control`` / ``role``) is left running — the
        registry owns its lifecycle across turns. Only this scope's presentation
        edge is torn down.
        """
        if self._state is ConnectionLifecycleState.CLOSED:
            return ConnectionCleanupReceipt(self._generation, self._state, (), ())
        self._state = ConnectionLifecycleState.DRAINING
        settled: list[ConnectionCleanupPhase] = []
        failed: list[ConnectionCleanupPhase] = []
        if ConnectionCleanupPhase.TELEMETRY in self._pending_cleanup:
            try:
                for handle in self._telemetry_handles:
                    await handle.aclose()
            except Exception:  # noqa: BLE001 - typed cleanup settlement below
                failed.append(ConnectionCleanupPhase.TELEMETRY)
            else:
                self._telemetry_handles.clear()
                self._pending_cleanup.remove(ConnectionCleanupPhase.TELEMETRY)
                settled.append(ConnectionCleanupPhase.TELEMETRY)
        if ConnectionCleanupPhase.HUMAN_BINDING in self._pending_cleanup:
            try:
                if self._human_token is not None:
                    self._role.reset_human_interaction(self._human_token)
                    self._human_token = None
            except Exception as exc:  # noqa: BLE001 - typed cleanup settlement below
                logger.warning(f"ConnectionScope: human binding cleanup failed: {exc}")
                failed.append(ConnectionCleanupPhase.HUMAN_BINDING)
            else:
                self._env_bound = False
                self._pending_cleanup.remove(ConnectionCleanupPhase.HUMAN_BINDING)
                settled.append(ConnectionCleanupPhase.HUMAN_BINDING)
        if ConnectionCleanupPhase.PROJECTOR in self._pending_cleanup:
            try:
                await self._projector.aclose()
            except Exception as exc:  # noqa: BLE001 - typed cleanup settlement below
                logger.warning(f"ConnectionScope: consumer close failed: {exc}")
                failed.append(ConnectionCleanupPhase.PROJECTOR)
            else:
                self._pending_cleanup.remove(ConnectionCleanupPhase.PROJECTOR)
                settled.append(ConnectionCleanupPhase.PROJECTOR)
        if ConnectionCleanupPhase.PORT in self._pending_cleanup and self._port is not None:
            try:
                await self._port.aclose()
            except Exception as exc:  # noqa: BLE001 - typed cleanup settlement below
                logger.warning(f"ConnectionScope: port cleanup failed: {exc}")
                failed.append(ConnectionCleanupPhase.PORT)
            else:
                self._pending_cleanup.remove(ConnectionCleanupPhase.PORT)
                settled.append(ConnectionCleanupPhase.PORT)
        if not self._pending_cleanup:
            self._state = ConnectionLifecycleState.CLOSED
        disposition = (
            ConnectionCloseDisposition.SETTLED
            if self._state is ConnectionLifecycleState.CLOSED
            else ConnectionCloseDisposition.CLEANUP_FAILED
        )
        return ConnectionCleanupReceipt(self._generation, self._state, tuple(settled), tuple(failed), disposition)


__all__ = [
    "ConnectionScope",
    "ConnectionLifecycleState",
    "ConnectionCleanupPhase",
    "ConnectionCleanupReceipt",
    "ConnectionCloseDisposition",
    "ConnectionTimeoutPolicy",
]

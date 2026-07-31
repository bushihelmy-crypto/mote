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

from typing import Any, List, Optional
from uuid import uuid4

from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.product.interaction.human_channel import PortHumanChannel
from mote.product.interaction.turn import TurnRunner, format_turn_error
from mote.product.presentation.input_events import is_presentation_input
from mote.product.presentation.projection.base import BaseProjector
from mote.product.presentation.projection.projector import ViewProjector
from mote.runtime.events import TelemetryHandle
from mote.runtime.telemetry.logging import logger

_format_turn_error = format_turn_error


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
        session: Any,
        *,
        consumers: Optional[List[Any]] = None,
        port: Any = None,
        quiescent_poll_interval: float = 0.05,
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
        self._telemetry_handle: Optional[TelemetryHandle] = None
        self._telemetry_identity = TelemetryIdentity(f"mote.product.cli.connection_scope.{uuid4().hex}")
        self._prev_env: Any = None
        self._env_bound = False

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

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def open(self) -> None:
        """Subscribe this scope's projector to telemetry and bind the port.

        Binding the human channel is scoped: the previous ``env`` is stashed and
        restored on :meth:`aclose`, so overlapping scopes on the same role (rare,
        but possible for a resumed thread) don't clobber each other's channel.
        """
        telemetry = getattr(self._role, "telemetry", None)
        if telemetry is not None and self._telemetry_handle is None:
            try:
                self._telemetry_handle = await telemetry.subscribe_typed(
                    TelemetrySubscriptionSpec(
                        identity=self._telemetry_identity,
                        capacity=4096,
                        overflow=TelemetryOverflow.DROP_OLDEST,
                    ),
                    is_presentation_input,
                    self._projector,
                    self._projector,
                )
            except Exception as exc:  # noqa: BLE001 — rendering is best-effort
                logger.warning(f"ConnectionScope: projector subscribe failed: {exc}")
        if self._port is not None:
            self._prev_env = getattr(self._role.state, "env", None)
            self._role.state.env = PortHumanChannel(self._port)
            self._env_bound = True

    async def run_turn(self, message: Any) -> None:
        """Drive one turn: inject *message*, await quiescence, fan output out.

        *message* is a backend ``UserMessage`` (built by the transport via
        ``backend.turn_message``). Surfaces the user's own turn as a
        ``MessageBlockCompleted`` ViewEvent first (symmetric with the terminal
        driver, so a network transcript shows the prompt), then delivers it to
        the control plane and polls to quiescence. A turn that ends ERRORED
        surfaces an ``ErrorRaised`` (never by reading history).
        """
        await self._turn_runner.run(message)

    async def aclose(self) -> None:
        """Unsubscribe the projector, restore the role env, close consumers/port.

        The shared engine (``control`` / ``role``) is left running — the
        registry owns its lifecycle across turns. Only this scope's presentation
        edge is torn down.
        """
        if self._telemetry_handle is not None:
            try:
                await self._telemetry_handle.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._telemetry_handle = None
        if self._env_bound:
            try:
                self._role.state.env = self._prev_env
            except Exception:  # noqa: BLE001
                pass
            self._env_bound = False
        for consumer in self._projector.consumers:
            aclose = getattr(consumer, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"ConnectionScope: consumer.aclose failed: {exc}")
        if self._port is not None:
            aclose = getattr(self._port, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass


__all__ = ["ConnectionScope"]

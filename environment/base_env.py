#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentEnvironment — the BaseEnvironment face over the agent control plane.

This is the bridge between the legacy single-bus ``BaseEnvironment`` contract
(``add_role`` / ``publish_message`` / ``roles`` / ``run``) and the codex-style
control plane built in this package. There is **no broadcast loop**: roles are
registered as :class:`AgentRuntime` instances on an :class:`AgentControl`, and
``publish_message`` routes by ``send_to`` addresses into the matching agents'
**mailboxes** (turn-atomic delivery), then ``run(k)`` pumps the scheduler.

Consumer surface kept intact for ``role.py`` / ``provider.py``:
  * ``desc`` (str), ``role_names()``, ``roles`` (dict name->Role),
  * ``set_addresses(role, addresses)`` — address→agent routing index,
  * ``publish_message(msg)`` — resolve recipients → control-plane delivery,
  * ``run(k=1)`` — bounded scheduler pump.

Residency-evicted agents do **not** appear in ``roles`` (mirrors codex
``live_agents``); they rehydrate transparently when a message is routed to them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from pydantic import ConfigDict, PrivateAttr

from metagpt.common.logs import logger
from metagpt.common.const import MESSAGE_ROUTE_TO_ALL
from metagpt.common.schema import Message
from metagpt.common.schema.env import BaseEnvAction, BaseEnvironment, BaseEnvObsParams
from metagpt.environment.control import AgentControl
from metagpt.common.exception import AgentLimitReached, AgentNotFound
from metagpt.environment.registry import AgentMetadata
from metagpt.environment.runtime import AgentRuntime
from metagpt.environment.store import ResidencyStore


class AgentEnvironment(BaseEnvironment):
    """A single-environment control plane exposed through the BaseEnvironment API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    desc: str = ""

    # Runtime-only state (never serialized).
    _control: AgentControl = PrivateAttr(default=None)
    _roles: Dict[str, Any] = PrivateAttr(default_factory=dict)  # name -> Role
    _addresses: Dict[str, Set[str]] = PrivateAttr(default_factory=dict)  # session_id -> addresses

    def model_post_init(self, __context: Any) -> None:
        if self._control is None:
            self._control = AgentControl(store=ResidencyStore())

    # ------------------------------------------------------------------
    # Plane accessors
    # ------------------------------------------------------------------
    @property
    def control(self) -> AgentControl:
        return self._control

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def add_role(self, role: Any) -> Any:
        """Register *role* as a live agent runtime on the control plane.

        Wraps the role in an :class:`AgentRuntime`, registers it, indexes it by
        name, then wires the env back onto the role (``role.set_env(self)``),
        which in turn pushes its addresses through :meth:`set_addresses`.
        """
        name = self._role_name(role)
        runtime = AgentRuntime(role)
        self._roles[name] = role
        self._control.add_agent(runtime, metadata=AgentMetadata(agent_nickname=name))
        # Seed the routing index (set_env -> set_addresses will refine it).
        addresses = getattr(getattr(role, "state", None), "addresses", None) or {name}
        self._addresses[role.session_id] = set(addresses)
        if hasattr(role, "set_env"):
            role.set_env(self)
        return role

    def add_roles(self, roles) -> None:
        for role in roles:
            self.add_role(role)

    def set_addresses(self, role: Any, addresses: Set[str]) -> None:
        """Update the address→agent routing index for *role* (codex address map)."""
        self._addresses[role.session_id] = set(addresses or [])

    # ------------------------------------------------------------------
    # Views consumed by provider.py
    # ------------------------------------------------------------------
    @property
    def roles(self) -> Dict[str, Any]:
        """Name→Role for currently *loaded* agents (evicted ones are omitted)."""
        live = self._control.runtimes()
        return {name: role for name, role in self._roles.items() if role.session_id in live}

    def role_names(self) -> list:
        return list(self.roles.keys())

    def get_role(self, name: str) -> Optional[Any]:
        return self.roles.get(name)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def publish_message(self, message: Message, peekable: bool = True) -> bool:
        """Route *message* to recipient agents' mailboxes via the control plane."""
        if not message:
            return False
        recipients = self._resolve_recipients(message.send_to)
        for session_id in recipients:
            try:
                self._control.send_input(session_id, message)
            except AgentLimitReached:
                # No execution slot right now: queue it so it lands at the next
                # turn boundary instead of dropping it.
                from metagpt.environment.mailbox import DeliveryMode

                self._control.send_input(session_id, message, mode=DeliveryMode.QUEUE_ONLY)
            except AgentNotFound:
                logger.warning(f"AgentEnvironment: recipient {session_id} not found; dropping message")
        return True

    def _resolve_recipients(self, send_to: Set[str]) -> list:
        """Map a message's ``send_to`` address set to recipient session ids.

        Broadcast (``MESSAGE_ROUTE_TO_ALL``) only reaches *loaded* agents. A
        targeted address, however, also resolves to an agent that has been
        residency-evicted to disk: routing to it rehydrates it transparently
        (the control plane's ``send_input`` loads it on the way in).
        """
        if MESSAGE_ROUTE_TO_ALL in send_to:
            return list(self._control.runtimes().keys())
        recipients = []
        for session_id, addresses in self._addresses.items():
            if addresses & send_to:
                recipients.append(session_id)
        return recipients

    # ------------------------------------------------------------------
    # Driving
    # ------------------------------------------------------------------
    async def run(self, k: int = 1) -> int:
        """Pump the scheduler up to *k* rounds (bounded barrier pump)."""
        return await self._control.run(k)

    def quiescent(self) -> bool:
        return self._control.quiescent()

    async def stop(self) -> None:
        await self._control.stop()

    # ------------------------------------------------------------------
    # Human channel (only MGXEnv has a real one; default is "unsupported")
    # ------------------------------------------------------------------
    async def ask_human(self, question: str, sent_from: Optional[Any] = None) -> str:
        """Default: this environment has no human channel."""
        return "Not in MGXEnv, command will not be executed."

    async def reply_to_human(self, content: str, sent_from: Optional[Any] = None) -> str:
        """Default: this environment has no human channel."""
        return "Not in MGXEnv, command will not be executed."

    # ------------------------------------------------------------------
    # BaseEnvironment gym-style stubs (unused by the react flow)
    # ------------------------------------------------------------------
    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None):
        return {}, {}

    def observe(self, obs_params: Optional[BaseEnvObsParams] = None) -> Any:
        return self.role_names()

    def step(self, action: BaseEnvAction):
        return {}, 0.0, False, False, {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _role_name(role: Any) -> str:
        schema = getattr(role, "role_schema", None)
        if schema is not None and getattr(schema, "name", None):
            return schema.name
        if getattr(role, "name", None):
            return role.name
        return role.session_id


__all__ = ["AgentEnvironment"]

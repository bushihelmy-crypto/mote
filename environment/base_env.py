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

from mote.common.exception import AgentNotFound
from mote.common.logs import logger
from mote.common.schema import Message
from mote.common.schema.env import BaseEnvironment
from mote.environment.control import AgentControl
from mote.environment.registry import AgentMetadata
from mote.environment.runtime import AgentRuntime
from mote.environment.store import ResidencyStore
from pydantic import ConfigDict, PrivateAttr


class AgentEnvironment(BaseEnvironment):
    """A single-environment control plane exposed through the BaseEnvironment API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    desc: str = ""

    # Runtime-only state (never serialized).
    _control: Optional[AgentControl] = PrivateAttr(default=None)
    _roles: Dict[str, Any] = PrivateAttr(default_factory=dict)  # name -> Role

    def model_post_init(self, __context: Any) -> None:
        if self._control is None:
            self._control = AgentControl(store=ResidencyStore())

    # ------------------------------------------------------------------
    # Plane accessors
    # ------------------------------------------------------------------
    @property
    def control(self) -> AgentControl:
        assert self._control is not None, "control accessed before model_post_init"
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
        self.control.add_agent(runtime, metadata=AgentMetadata(agent_nickname=name))
        # Wire the explicit plane reference onto the role's context so spawn
        # sites holding it (skill forks) reach the live plane directly; turns
        # driven through the scheduler also bind it ambiently.
        ctx = getattr(role, "_context", None)
        if ctx is not None and getattr(ctx, "agent_control", None) is None:
            ctx.agent_control = self.control
        # Seed the routing index (set_env -> set_addresses will refine it).
        addresses = getattr(getattr(role, "state", None), "addresses", None) or {name}
        self.control.comm_graph.set_addresses(role.session_id, set(addresses))
        if hasattr(role, "set_env"):
            role.set_env(self)
        return role

    def add_roles(self, roles) -> None:
        for role in roles:
            self.add_role(role)

    def set_addresses(self, role: Any, addresses: Set[str]) -> None:
        """Update the address→agent routing index for *role* (codex address map)."""
        self.control.comm_graph.set_addresses(role.session_id, set(addresses or []))

    # ------------------------------------------------------------------
    # Views consumed by provider.py
    # ------------------------------------------------------------------
    @property
    def roles(self) -> Dict[str, Any]:
        """Name→Role for currently *loaded* agents (evicted ones are omitted)."""
        live = self.control.runtimes()
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
                self.control.send_input(session_id, message)
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
        return self.control.comm_graph.resolve_recipients(send_to, all_ids=self.control.runtimes().keys())

    # ------------------------------------------------------------------
    # Driving
    # ------------------------------------------------------------------
    async def run(self, k: int = 1) -> int:
        """Pump the scheduler up to *k* rounds (bounded barrier pump)."""
        return await self.control.run(k)

    def quiescent(self) -> bool:
        return self.control.quiescent()

    async def stop(self) -> None:
        await self.control.stop()

    # ------------------------------------------------------------------
    # Human channel (only MoteEnv has a real one; default is "unsupported")
    # ------------------------------------------------------------------
    async def ask_human(self, question: str, sent_from: Optional[Any] = None) -> str:
        """Default: this environment has no human channel."""
        return "Not in MoteEnv, command will not be executed."

    async def ask_user_question(self, questions: Any, sent_from: Optional[Any] = None) -> Any:
        """Default: no human channel → empty structured answers.

        The production front-end is ``PortHumanChannel`` (mote.cli), which
        overrides this to route to a port's ``ask_questions``. A non-CLI
        environment returning empty answers is a deliberate decision, not an
        oversight; MoteEnv may optionally override to walk ``get_human_input``.
        """
        from mote.common.schema import AskUserQuestionAnswers

        return AskUserQuestionAnswers()

    async def reply_to_human(self, content: str, sent_from: Optional[Any] = None) -> str:
        """Default: this environment has no human channel."""
        return "Not in MoteEnv, command will not be executed."

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
